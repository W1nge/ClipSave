from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .bulk_checkpoint import BulkImageCheckpoint, save_checkpoint
from .database import LibraryDatabase
from .services import OperationCancelled, preflight_image_file


class ImageAnalysisService(Protocol):
    def ocr_image(self, image, cancel_event, *, expected_sha256: str) -> str: ...

    def describe_image(self, image, cancel_event, *, expected_sha256: str) -> str: ...


@dataclass(frozen=True, slots=True)
class BulkImageResult:
    total: int
    processed: int
    completed: int
    skipped: int
    failed: int
    cancelled: bool = False
    error: str = ""


ProgressCallback = Callable[[int, int, int, str], None]


class BulkImageJob:
    """Execute a resumable OCR + description pass over library images."""

    def __init__(
        self,
        database: LibraryDatabase,
        service: ImageAnalysisService,
        checkpoint_path: Path,
    ) -> None:
        self.database = database
        self.service = service
        self.checkpoint_path = Path(checkpoint_path)

    def run(
        self,
        checkpoint: BulkImageCheckpoint,
        cancel_event: threading.Event,
        progress: ProgressCallback,
    ) -> BulkImageResult:
        current = checkpoint
        total = current.total
        cancelled = False
        error = ""

        while current.processed < total:
            if cancel_event.is_set():
                cancelled = True
                break
            item_id = current.current_item_id
            if item_id is None:
                break
            try:
                item = self.database.get_item(item_id)
            except Exception as exc:
                error = f"读取图片索引失败：{exc}"
                break
            if not item or item["kind"] != "image" or not item["path"] or not item["content_hash"]:
                current, checkpoint_error = self._advance_and_save(current, "skipped")
                if checkpoint_error:
                    error = checkpoint_error
                    break
                progress(current.processed, total, item_id, "已跳过")
                continue
            try:
                image_snapshot = preflight_image_file(Path(item["path"]))
            except Exception:
                current, checkpoint_error = self._advance_and_save(current, "failed")
                if checkpoint_error:
                    error = checkpoint_error
                    break
                progress(current.processed, total, item_id, "本地文件不可用")
                continue

            try:
                if current.stage == "ocr":
                    progress(current.processed, total, item_id, "正在 OCR")
                    ocr_text = self.service.ocr_image(
                        image_snapshot,
                        cancel_event,
                        expected_sha256=item["content_hash"],
                    )
                    image_snapshot.require_current()
                    if not self.database.update_ocr_if_current(
                        item_id,
                        item["content_hash"],
                        ocr_text,
                    ):
                        current, checkpoint_error = self._advance_and_save(current, "skipped")
                        if checkpoint_error:
                            error = checkpoint_error
                            break
                        progress(current.processed, total, item_id, "图片已变化")
                        continue
                    current = current.at_stage("description")
                    save_checkpoint(self.checkpoint_path, current)

                progress(current.processed, total, item_id, "正在生成描述")
                description = self.service.describe_image(
                    image_snapshot,
                    cancel_event,
                    expected_sha256=item["content_hash"],
                )
                image_snapshot.require_current()
                if not self.database.update_ai_if_current(
                    item_id,
                    item["content_hash"],
                    description,
                ):
                    current, checkpoint_error = self._advance_and_save(current, "skipped")
                    if checkpoint_error:
                        error = checkpoint_error
                        break
                    progress(current.processed, total, item_id, "图片已变化")
                    continue
            except OperationCancelled:
                cancelled = True
                break
            except Exception as exc:
                error = str(exc)
                break

            current, checkpoint_error = self._advance_and_save(current, "completed")
            if checkpoint_error:
                error = checkpoint_error
                break
            progress(current.processed, total, item_id, "已完成")

        return BulkImageResult(
            total=total,
            processed=current.processed,
            completed=current.completed,
            skipped=current.skipped,
            failed=current.failed,
            cancelled=cancelled,
            error=error,
        )

    def _advance_and_save(
        self,
        checkpoint: BulkImageCheckpoint,
        outcome: str,
    ) -> tuple[BulkImageCheckpoint, str]:
        updated = checkpoint.advance(outcome)
        try:
            save_checkpoint(self.checkpoint_path, updated)
        except OSError as exc:
            return updated, f"无法保存批处理断点：{exc}"
        return updated, ""
