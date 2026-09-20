from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .database import LibraryDatabase


@dataclass(frozen=True, slots=True)
class MetadataMutationResult:
    value: object = None
    error: Exception | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None


class LibraryMetadataController:
    """Own synchronous item metadata and classification mutations."""

    def __init__(self, database: LibraryDatabase) -> None:
        self.database = database

    def set_favorite(self, item_id: int, value: bool) -> MetadataMutationResult:
        return self._run(self.database.set_favorite, item_id, value)

    def set_notes(self, item_id: int, notes: str) -> MetadataMutationResult:
        return self._run(self.database.set_notes, item_id, notes)

    def create_collection(self, name: str) -> MetadataMutationResult:
        return self._run(self.database.create_collection, name)

    def delete_collection(self, collection_id: int) -> MetadataMutationResult:
        return self._run(self.database.delete_collection, collection_id)

    def delete_tag(self, tag_id: int) -> MetadataMutationResult:
        return self._run(self.database.delete_tag, tag_id)

    def add_tag(self, item_id: int, name: str) -> MetadataMutationResult:
        return self._run(self.database.add_tag, item_id, name)

    def remove_tag(self, item_id: int, name: str) -> MetadataMutationResult:
        return self._run(self.database.remove_tag, item_id, name)

    def set_collection(
        self,
        item_id: int,
        collection_id: int | None,
    ) -> MetadataMutationResult:
        return self._run(self.database.set_collection, item_id, collection_id)

    @staticmethod
    def _run(action: Callable[..., Any], *args) -> MetadataMutationResult:
        try:
            return MetadataMutationResult(value=action(*args))
        except Exception as exc:
            return MetadataMutationResult(error=exc)
