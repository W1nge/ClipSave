from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path


CHECKPOINT_FILENAME = "bulk-image-job.json"
CHECKPOINT_VERSION = 1
MAX_CHECKPOINT_BYTES = 16 * 1024 * 1024
MAX_CHECKPOINT_ITEMS = 1_000_000
_STAGES = {"ocr", "description"}
_OUTCOMES = {"completed", "skipped", "failed"}


@dataclass(frozen=True, slots=True)
class BulkImageCheckpoint:
    image_ids: tuple[int, ...]
    next_index: int = 0
    stage: str = "ocr"
    completed: int = 0
    skipped: int = 0
    failed: int = 0
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
        return replace(self, stage=stage)

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
    expected_keys = {
        "version",
        "image_ids",
        "next_index",
        "stage",
        "completed",
        "skipped",
        "failed",
    }
    if set(value) != expected_keys or not isinstance(value.get("image_ids"), list):
        raise ValueError("Bulk image checkpoint has an invalid structure")
    checkpoint = BulkImageCheckpoint(
        version=value["version"],
        image_ids=tuple(value["image_ids"]),
        next_index=value["next_index"],
        stage=value["stage"],
        completed=value["completed"],
        skipped=value["skipped"],
        failed=value["failed"],
    )
    _validate_checkpoint(checkpoint)
    return checkpoint


def save_checkpoint(path: Path, checkpoint: BulkImageCheckpoint) -> None:
    _validate_checkpoint(checkpoint)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(checkpoint)
    payload["image_ids"] = list(checkpoint.image_ids)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
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
