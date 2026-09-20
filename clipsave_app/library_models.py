from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, fields
from typing import Any, ClassVar


class RecordMapping(Mapping[str, object]):
    """Mapping-compatible DTO base used during the sqlite.Row migration."""

    _field_names: ClassVar[tuple[str, ...]]

    def __getitem__(self, key: str) -> object:
        if key not in self._field_names:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._field_names)

    def __len__(self) -> int:
        return len(self._field_names)

    def keys(self) -> tuple[str, ...]:
        return self._field_names

    @classmethod
    def _init_field_names(cls) -> None:
        cls._field_names = tuple(item.name for item in fields(cls))


@dataclass(frozen=True, slots=True)
class LibraryItem(RecordMapping):
    id: int
    kind: str
    title: str
    content: str | None
    path: str | None
    resolved_path: str | None
    mime: str | None
    content_hash: str | None
    created_at: str
    updated_at: str | None
    file_size: int
    width: int | None
    height: int | None
    source: str | None
    favorite: int
    notes: str | None
    ocr_text: str | None
    ai_description: str | None
    embedding: str | None
    embedding_provider: str | None
    embedding_model: str | None
    embedding_dimensions: int | None
    embedding_revision: int | None
    collection_id: int | None
    external: int
    missing: int
    collection_name: str | None
    tag_names: str | None
    tag_colors: str | None

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "LibraryItem":
        return cls(**{name: row[name] for name in cls._field_names})


LibraryItem._init_field_names()


@dataclass(frozen=True, slots=True)
class CollectionSummary(RecordMapping):
    id: int
    name: str
    created_at: str
    amount: int

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "CollectionSummary":
        return cls(**{name: row[name] for name in cls._field_names})


CollectionSummary._init_field_names()


@dataclass(frozen=True, slots=True)
class TagSummary(RecordMapping):
    id: int
    name: str
    color: str
    amount: int

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "TagSummary":
        return cls(**{name: row[name] for name in cls._field_names})


TagSummary._init_field_names()


@dataclass(slots=True)
class LibraryViewState:
    items: list[object] = field(default_factory=list)
    offset: int = 0
    has_more: bool = False
    loading: bool = False
    selected_item_id: int | None = None
    kind: str | None = None
    favorite: bool = False
    day: str | None = None
    recent: bool = False
    collection_id: int | None = None
    tag_id: int | None = None
    sort: str = "newest"


@dataclass(frozen=True, slots=True)
class LibraryQuery:
    query: str = ""
    query_terms: tuple[str, ...] | None = None
    kind: str | None = None
    favorite: bool = False
    day: str | None = None
    recent_days: int | None = None
    collection_id: int | None = None
    tag_id: int | None = None
    sort: str = "newest"

    def as_database_kwargs(self) -> dict[str, object]:
        return {
            "query": self.query,
            "query_terms": self.query_terms,
            "kind": self.kind,
            "favorite": self.favorite,
            "day": self.day,
            "recent_days": self.recent_days,
            "collection_id": self.collection_id,
            "tag_id": self.tag_id,
            "sort": self.sort,
        }
