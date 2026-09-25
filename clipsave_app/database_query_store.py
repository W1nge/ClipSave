from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager

from .library_models import CollectionSummary, LibraryItem, TagSummary


class DatabaseQueryStore:
    """Read-model queries for the ClipSave library database."""

    LAST_USED_EXPRESSION = (
        "COALESCE((SELECT u.last_used_at FROM usage.item_usage u"
        " WHERE u.item_id = i.id), rtrim(i.created_at, 'Z') || '.000000Z')"
    )

    def __init__(
        self,
        *,
        connection: Callable[[], sqlite3.Connection],
        lock: AbstractContextManager,
        utc_timestamp: Callable[[dt.datetime | str | None], str],
        summary_content_limit: int,
        max_search_terms: int,
    ) -> None:
        self._connection = connection
        self._lock = lock
        self._utc_timestamp = utc_timestamp
        self.summary_content_limit = int(summary_content_limit)
        self.max_search_terms = int(max_search_terms)

    @staticmethod
    def item_order(sort: str) -> str:
        orders = {
            "newest": f"{DatabaseQueryStore.LAST_USED_EXPRESSION} DESC, i.id DESC",
            "oldest": "i.created_at ASC, i.id ASC",
            "name": "i.title COLLATE NOCASE ASC, i.id ASC",
            "size": "i.file_size DESC, i.id DESC",
            "type": "i.kind ASC, i.created_at DESC, i.id DESC",
        }
        return orders.get(sort, orders["newest"])

    def query_items(
        self,
        query: str = "",
        kind: str | None = None,
        favorite: bool = False,
        day: str | None = None,
        recent_days: int | None = None,
        collection_id: int | None = None,
        tag_id: int | None = None,
        sort: str = "newest",
        summary_only: bool = False,
        limit: int | None = None,
        offset: int = 0,
        query_terms: Iterable[str] | None = None,
    ) -> list[LibraryItem]:
        if limit is not None and (not isinstance(limit, int) or limit < 0):
            raise ValueError("limit must be a non-negative integer or None")
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        joins, clauses, parameters = self._query_filter(
            query=query,
            kind=kind,
            favorite=favorite,
            day=day,
            recent_days=recent_days,
            collection_id=collection_id,
            tag_id=tag_id,
            query_terms=query_terms,
        )
        projection = f"i.*, {self.LAST_USED_EXPRESSION} AS last_used_at"
        if summary_only:
            projection = f"""
                i.id, i.kind, i.title, substr(i.content,1,{self.summary_content_limit}) AS content,
                i.path, i.resolved_path, i.mime, i.content_hash, i.created_at, i.updated_at,
                {self.LAST_USED_EXPRESSION} AS last_used_at,
                i.file_size, i.width, i.height, i.source, i.favorite, '' AS notes,
                '' AS ocr_text, '' AS ai_description, NULL AS embedding,
                i.embedding_provider, i.embedding_model, i.embedding_dimensions,
                i.embedding_revision, i.collection_id,
                i.external, i.missing
            """
        pagination = ""
        if limit is not None:
            pagination = "LIMIT ? OFFSET ?"
            parameters.extend((limit, offset))
        elif offset:
            pagination = "LIMIT -1 OFFSET ?"
            parameters.append(offset)
        sql = f"""
            SELECT {projection}, c.name AS collection_name,
                   (SELECT GROUP_CONCAT(name, char(31)) FROM (
                       SELECT tag.name AS name
                       FROM item_tags link JOIN tags tag ON tag.id=link.tag_id
                       WHERE link.item_id=i.id ORDER BY tag.id
                   )) AS tag_names,
                   (SELECT GROUP_CONCAT(color, char(31)) FROM (
                       SELECT tag.color AS color
                       FROM item_tags link JOIN tags tag ON tag.id=link.tag_id
                       WHERE link.item_id=i.id ORDER BY tag.id
                   )) AS tag_colors
            FROM items i
            LEFT JOIN collections c ON c.id = i.collection_id
            {joins}
            WHERE {' AND '.join(clauses)}
            ORDER BY {self.item_order(sort)}
            {pagination}
        """
        with self._lock:
            rows = self._connection().execute(sql, parameters).fetchall()
        return [LibraryItem.from_mapping(row) for row in rows]

    def _query_filter(
        self,
        *,
        query: str = "",
        kind: str | None = None,
        favorite: bool = False,
        day: str | None = None,
        recent_days: int | None = None,
        collection_id: int | None = None,
        tag_id: int | None = None,
        query_terms: Iterable[str] | None = None,
    ) -> tuple[str, list[str], list[object]]:
        clauses = ["i.missing = 0"]
        parameters: list[object] = []
        joins = ""
        if query_terms is None:
            search_terms = [query] if query else []
        else:
            if isinstance(query_terms, (str, bytes)):
                raise TypeError("query_terms must be an iterable of strings")
            search_terms = []
            seen_terms: set[str] = set()
            for value in query_terms:
                if not isinstance(value, str):
                    raise TypeError("query_terms must contain only strings")
                term = value.strip()
                if not term:
                    continue
                key = term.casefold()
                if key in seen_terms:
                    continue
                seen_terms.add(key)
                search_terms.append(term)
                if len(search_terms) > self.max_search_terms:
                    raise ValueError(
                        f"query_terms cannot contain more than {self.max_search_terms} terms"
                    )
            if not search_terms and query:
                search_terms.append(query)
        if search_terms:
            term_clauses = []
            for term in search_terms:
                term_clauses.append(
                    """(
                        i.title LIKE ? ESCAPE '\\' OR i.content LIKE ? ESCAPE '\\'
                        OR i.ocr_text LIKE ? ESCAPE '\\' OR i.ai_description LIKE ? ESCAPE '\\'
                        OR i.notes LIKE ? ESCAPE '\\'
                        OR EXISTS(
                            SELECT 1
                            FROM item_tags search_link
                            JOIN tags search_tag ON search_tag.id=search_link.tag_id
                            WHERE search_link.item_id=i.id AND search_tag.name LIKE ? ESCAPE '\\'
                        )
                    )"""
                )
                escaped_term = (
                    term.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                token = f"%{escaped_term}%"
                parameters.extend([token] * 6)
            clauses.append(f"({' OR '.join(term_clauses)})")
        if kind:
            clauses.append("i.kind = ?")
            parameters.append(kind)
        if favorite:
            clauses.append("i.favorite = 1")
        if day:
            clauses.append("date(i.created_at, 'localtime') = ?")
            parameters.append(day)
        if recent_days:
            cutoff = self._utc_timestamp(
                dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=recent_days)
            )
            clauses.append("i.created_at >= ?")
            parameters.append(cutoff)
        if collection_id is not None:
            clauses.append("i.collection_id = ?")
            parameters.append(collection_id)
        if tag_id is not None:
            joins += " JOIN item_tags filter_tags ON filter_tags.item_id = i.id "
            clauses.append("filter_tags.tag_id = ?")
            parameters.append(tag_id)
        return joins, clauses, parameters

    def count_query_items(
        self,
        query: str = "",
        kind: str | None = None,
        favorite: bool = False,
        day: str | None = None,
        recent_days: int | None = None,
        collection_id: int | None = None,
        tag_id: int | None = None,
        query_terms: Iterable[str] | None = None,
    ) -> int:
        joins, clauses, parameters = self._query_filter(
            query=query,
            kind=kind,
            favorite=favorite,
            day=day,
            recent_days=recent_days,
            collection_id=collection_id,
            tag_id=tag_id,
            query_terms=query_terms,
        )
        with self._lock:
            return int(
                self._connection()
                .execute(
                    f"""
                    SELECT COUNT(DISTINCT i.id)
                    FROM items i
                    {joins}
                    WHERE {' AND '.join(clauses)}
                    """,
                    parameters,
                )
                .fetchone()[0]
            )

    def count_items(self, *, kind: str | None = None) -> int:
        clauses = ["missing = 0"]
        parameters: list[object] = []
        if kind is not None:
            clauses.append("kind = ?")
            parameters.append(kind)
        with self._lock:
            return int(
                self._connection()
                .execute(
                    f"SELECT COUNT(*) FROM items WHERE {' AND '.join(clauses)}",
                    parameters,
                )
                .fetchone()[0]
            )

    def item_ids(self, *, kind: str | None = None, sort: str = "newest") -> list[int]:
        clauses = ["i.missing = 0"]
        parameters: list[object] = []
        if kind is not None:
            clauses.append("i.kind = ?")
            parameters.append(kind)
        with self._lock:
            return [
                int(row[0])
                for row in self._connection()
                .execute(
                    f"""
                    SELECT i.id
                    FROM items i
                    WHERE {' AND '.join(clauses)}
                    ORDER BY {self.item_order(sort)}
                    """,
                    parameters,
                )
                .fetchall()
            ]

    def get_item(self, item_id: int) -> LibraryItem | None:
        with self._lock:
            row = self._connection().execute(
                f"""
                SELECT i.*, {self.LAST_USED_EXPRESSION} AS last_used_at, c.name AS collection_name,
                       (SELECT GROUP_CONCAT(name, char(31)) FROM (
                           SELECT tag.name AS name
                           FROM item_tags link JOIN tags tag ON tag.id=link.tag_id
                           WHERE link.item_id=i.id ORDER BY tag.id
                       )) AS tag_names,
                       (SELECT GROUP_CONCAT(color, char(31)) FROM (
                           SELECT tag.color AS color
                           FROM item_tags link JOIN tags tag ON tag.id=link.tag_id
                           WHERE link.item_id=i.id ORDER BY tag.id
                       )) AS tag_colors
                FROM items i
                LEFT JOIN collections c ON c.id = i.collection_id
                WHERE i.id = ? AND i.missing = 0
                """,
                (item_id,),
            ).fetchone()
        return None if row is None else LibraryItem.from_mapping(row)

    def counts(self) -> dict[str, int]:
        with self._lock:
            connection = self._connection()
            rows = connection.execute(
                "SELECT kind, COUNT(*) amount FROM items WHERE missing=0 GROUP BY kind"
            ).fetchall()
            result = {
                "all": 0,
                "image": 0,
                "text": 0,
                "markdown": 0,
                "favorite": 0,
            }
            for row in rows:
                result[row["kind"]] = row["amount"]
                result["all"] += row["amount"]
            result["favorite"] = connection.execute(
                "SELECT COUNT(*) FROM items WHERE favorite=1 AND missing=0"
            ).fetchone()[0]
            return result

    def days(self) -> list[tuple[str, int]]:
        with self._lock:
            return [
                (row["day"], row["amount"])
                for row in self._connection()
                .execute(
                    "SELECT date(created_at, 'localtime') day, COUNT(*) amount "
                    "FROM items WHERE missing=0 GROUP BY day ORDER BY day DESC"
                )
                .fetchall()
            ]

    def collections(self) -> list[CollectionSummary]:
        with self._lock:
            rows = self._connection().execute(
                """
                SELECT c.*, COUNT(i.id) amount
                FROM collections c
                LEFT JOIN items i ON i.collection_id=c.id AND i.missing=0
                GROUP BY c.id
                ORDER BY c.name COLLATE NOCASE, c.id
                """
            ).fetchall()
        return [CollectionSummary.from_mapping(row) for row in rows]

    def tags(self) -> list[TagSummary]:
        with self._lock:
            rows = self._connection().execute(
                """
                SELECT t.*, COUNT(i.id) amount
                FROM tags t
                LEFT JOIN item_tags it ON it.tag_id=t.id
                LEFT JOIN items i ON i.id=it.item_id AND i.missing=0
                GROUP BY t.id
                ORDER BY t.name COLLATE NOCASE, t.id
                """
            ).fetchall()
        return [TagSummary.from_mapping(row) for row in rows]
