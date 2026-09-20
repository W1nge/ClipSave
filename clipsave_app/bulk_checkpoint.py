from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .atomic_files import sync_directory, write_json_temp


CHECKPOINT_FILENAME = "bulk-image-job.json"
CHECKPOINT_VERSION = 2
MAX_CHECKPOINT_BYTES = 32 * 1024 * 1024
MAX_CHECKPOINT_ITEMS = 1_000_000
_STAGES = {"ocr", "description"}
_OUTCOMES = {"completed", "skipped", "failed"}
_HEX = frozenset("0123456789abcdefABCDEF")


@dataclass(frozen=True, slots=True)
class BulkImageCheckpoint:
    image_ids: tuple[int, ...]
    next_index: int = 0
    stage: str = "ocr"
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    pending_stage: str = ""
    pending_text: str = ""
    pending_content_hash: str = ""
    version: int = CHECKPOINT_VERSION

    @property
    def total(self) -> int:
        return len(self.image_ids)

    @property
    def processed(self) -> int:
        return self.next_index

    @property
    def current_item_id(self) -> int | None:
        if self.next_index >= self.total:
            return None
        return self.image_ids[self.next_index]

    def at_stage(self, stage: str) -> "BulkImageCheckpoint":
        if stage not in _STAGES:
            raise ValueError(f"Invalid bulk image stage: {stage}")
        return replace(
            self,
            stage=stage,
            pending_stage="",
            pending_text="",
            pending_content_hash="",
        )

    def with_pending(
        self,
        stage: str,
        text: str,
        content_hash: str,
    ) -> "BulkImageCheckpoint":
        if stage != self.stage or stage not in _STAGES:
            raise ValueError("Pending bulk result does not match the current stage")
        checkpoint = replace(
            self,
            pending_stage=stage,
            pending_text=text,
            pending_content_hash=content_hash,
        )
        _validate_checkpoint(checkpoint)
        return checkpoint

    def clear_pending(self) -> "BulkImageCheckpoint":
        if not self.pending_stage and not self.pending_text and not self.pending_content_hash:
            return self
        return replace(
            self,
            pending_stage="",
            pending_text="",
            pending_content_hash="",
        )

    def advance(self, outcome: str) -> "BulkImageCheckpoint":
        if outcome not in _OUTCOMES:
            raise ValueError(f"Invalid bulk image outcome: {outcome}")
        if self.next_index >= self.total:
            raise ValueError("Bulk image checkpoint is already complete")
        counters = {
            "completed": self.completed,
            "skipped": self.skipped,
            "failed": self.failed,
        }
        counters[outcome] += 1
        return replace(
            self,
            next_index=self.next_index + 1,
            stage="ocr",
            pending_stage="",
            pending_text="",
            pending_content_hash="",
            **counters,
        )


def checkpoint_path(settings_path: Path) -> Path:
    return settings_path.parent / CHECKPOINT_FILENAME


def new_checkpoint(image_ids: list[int] | tuple[int, ...]) -> BulkImageCheckpoint:
    checkpoint = BulkImageCheckpoint(tuple(image_ids))
    _validate_checkpoint(checkpoint)
    return checkpoint


def load_checkpoint(path: Path) -> BulkImageCheckpoint | None:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None
    if size <= 0 or size > MAX_CHECKPOINT_BYTES:
        raise ValueError("Bulk image checkpoint has an invalid size")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Bulk image checkpoint cannot be read") from exc
    if not isinstance(value, dict):
        raise ValueError("Bulk image checkpoint must be an object")
    version = value.get("version")
    legacy_keys = {
        "version",
        "image_ids",
        "next_index",
        "stage",
        "completed",
        "skipped",
        "failed",
    }
    current_keys = legacy_keys | {
        "pending_stage",
        "pending_text",
        "pending_content_hash",
    }
    expected_keys = legacy_keys if version == 1 else current_keys
    if (
        type(version) is not int
        or version not in {1, CHECKPOINT_VERSION}
        or set(value) != expected_keys
        or not isinstance(value.get("image_ids"), list)
    ):
        raise ValueError("Bulk image checkpoint has an invalid structure")
    checkpoint = BulkImageCheckpoint(
        version=CHECKPOINT_VERSION,
        image_ids=tuple(value["image_ids"]),
        next_index=value["next_index"],
        stage=value["stage"],
        completed=value["completed"],
        skipped=value["skipped"],
        failed=value["failed"],
        pending_stage=value.get("pending_stage", ""),
        pending_text=value.get("pending_text", ""),
        pending_content_hash=value.get("pending_content_hash", ""),
    )
    _validate_checkpoint(checkpoint)
    return checkpoint


def save_checkpoint(path: Path, checkpoint: BulkImageCheckpoint) -> None:
    _validate_checkpoint(checkpoint)
    payload = asdict(checkpoint)
    payload["image_ids"] = list(checkpoint.image_ids)
    temporary = write_json_temp(
        path,
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def clear_checkpoint(path: Path) -> None:
    path.unlink(missing_ok=True)


def _validate_checkpoint(checkpoint: BulkImageCheckpoint) -> None:
    if type(checkpoint.version) is not int or checkpoint.version != CHECKPOINT_VERSION:
        raise ValueError("Unsupported bulk image checkpoint version")
    if not checkpoint.image_ids or len(checkpoint.image_ids) > MAX_CHECKPOINT_ITEMS:
        raise ValueError("Bulk image checkpoint has an invalid item count")
    if any(type(item_id) is not int or item_id <= 0 for item_id in checkpoint.image_ids):
        raise ValueError("Bulk image checkpoint contains an invalid item ID")
    if len(set(checkpoint.image_ids)) != len(checkpoint.image_ids):
        raise ValueError("Bulk image checkpoint contains duplicate item IDs")
    if type(checkpoint.next_index) is not int or not 0 <= checkpoint.next_index <= checkpoint.total:
        raise ValueError("Bulk image checkpoint has an invalid position")
    if checkpoint.stage not in _STAGES:
        raise ValueError("Bulk image checkpoint has an invalid stage")
    counters = (checkpoint.completed, checkpoint.skipped, checkpoint.failed)
    if any(type(value) is not int or value < 0 for value in counters):
        raise ValueError("Bulk image checkpoint has invalid counters")
    if sum(counters) != checkpoint.next_index:
        raise ValueError("Bulk image checkpoint counters do not match its position")
    if checkpoint.next_index == checkpoint.total and checkpoint.stage != "ocr":
        raise ValueError("Completed bulk image checkpoint has an invalid stage")
    if checkpoint.pending_stage:
        if checkpoint.pending_stage != checkpoint.stage:
            raise ValueError("Bulk image checkpoint pending stage is inconsistent")
        if type(checkpoint.pending_text) is not str:
            raise ValueError("Bulk image checkpoint pending text is invalid")
        digest = checkpoint.pending_content_hash
        if (
            type(digest) is not str
            or len(digest) != 64
            or any(character not in _HEX for character in digest)
        ):
            raise ValueError("Bulk image checkpoint pending hash is invalid")
    elif checkpoint.pending_text or checkpoint.pending_content_hash:
        raise ValueError("Bulk image checkpoint has orphaned pending data")
