from __future__ import annotations

import os

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from .constants import TYPE_LABELS


def normalized_thumbnail_path(path) -> str | None:
    if not path:
        return None
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _human_size(value: int) -> str:
    amount = float(value or 0)
    for suffix in ("B", "KB", "MB", "GB"):
        if amount < 1024 or suffix == "GB":
            return f"{amount:.0f} {suffix}" if suffix == "B" else f"{amount:.1f} {suffix}"
        amount /= 1024
    return "0 B"


class AssetItemModel(QAbstractTableModel):
    ItemRole = int(Qt.ItemDataRole.UserRole) + 1
    IdRole = ItemRole + 1
    FavoriteRole = ItemRole + 2
    ThumbnailPathRole = ItemRole + 3
    GenerationRole = ItemRole + 4

    HEADERS = ("名称", "类型", "标签", "捕获时间", "大小")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list = []
        self._rows_by_id: dict[int, int] = {}
        self._rows_by_thumbnail_path: dict[str, list[int]] = {}
        self._generation = 0

    def set_items(self, items) -> None:
        records = list(items)
        self.beginResetModel()
        self._generation += 1
        self.items = records
        self._rows_by_id = {int(record["id"]): row for row, record in enumerate(records)}
        rows_by_thumbnail_path: dict[str, list[int]] = {}
        for row, record in enumerate(records):
            if record["kind"] != "image":
                continue
            path = normalized_thumbnail_path(record["path"])
            if path is not None:
                rows_by_thumbnail_path.setdefault(path, []).append(row)
        self._rows_by_thumbnail_path = rows_by_thumbnail_path
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.items):
            return None
        record = self.items[index.row()]
        if role == self.ItemRole:
            return record
        if role == self.IdRole:
            return int(record["id"])
        if role == self.FavoriteRole:
            return bool(record["favorite"])
        if role == self.ThumbnailPathRole:
            return normalized_thumbnail_path(record["path"]) if record["kind"] == "image" else None
        if role == self.GenerationRole:
            return self._generation
        if role == Qt.ItemDataRole.ToolTipRole and index.column() == 0:
            return str(record["title"])
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if index.column() == 0:
            return str(record["title"])
        if index.column() == 1:
            kind = record["kind"]
            return TYPE_LABELS.get(kind, kind)
        if index.column() == 2:
            return (record["tag_names"] or "").replace("\x1f", ", ")
        if index.column() == 3:
            return str(record["created_at"]).replace("T", " ")
        if index.column() == 4:
            return _human_size(record["file_size"])
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
            and 0 <= section < len(self.HEADERS)
        ):
            return self.HEADERS[section]
        return super().headerData(section, orientation, role)

    def item(self, row: int):
        if 0 <= row < len(self.items):
            return self.items[row]
        return None

    def row_for_id(self, item_id: int | None) -> int:
        if item_id is None:
            return -1
        return self._rows_by_id.get(int(item_id), -1)

    @property
    def generation(self) -> int:
        return self._generation

    def has_thumbnail_path(self, path, generation: int) -> bool:
        normalized = normalized_thumbnail_path(path)
        return generation == self._generation and normalized in self._rows_by_thumbnail_path

    def notify_thumbnail_changed(self, path, generation: int) -> bool:
        normalized = normalized_thumbnail_path(path)
        if generation != self._generation or normalized is None:
            return False
        rows = self._rows_by_thumbnail_path.get(normalized)
        if not rows:
            return False
        self.dataChanged.emit(
            self.index(rows[0], 0),
            self.index(rows[-1], 0),
            [Qt.ItemDataRole.DecorationRole],
        )
        return True
