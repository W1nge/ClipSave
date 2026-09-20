from __future__ import annotations

from PySide6.QtCore import QModelIndex, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QStyle,
    QStyleOptionViewItem,
    QStyledItemDelegate,
    QTableView,
)

from .item_gestures import _ItemRightClickGesture, _ItemTripleClickGesture
from .item_models import AssetItemModel
from .ui_primitives import AutoHideScrollBar, _WheelRemainder, _half_speed_wheel_event


class _AssetTableFavoriteDelegate(QStyledItemDelegate):
    @staticmethod
    def favorite_rect(cell: QRect) -> QRect:
        return QRect(cell.right() - 34, cell.top(), 34, cell.height())

    def paint(self, painter: QPainter, option, index) -> None:
        if index.column() != 0:
            super().paint(painter, option, index)
            return
        base_option = QStyleOptionViewItem(option)
        self.initStyleOption(base_option, index)
        title = base_option.text
        base_option.text = ""
        widget = base_option.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem,
            base_option,
            painter,
            widget,
        )
        painter.save()
        selected = bool(base_option.state & QStyle.StateFlag.State_Selected)
        painter.setPen(
            base_option.palette.highlightedText().color()
            if selected
            else base_option.palette.text().color()
        )
        text_rect = base_option.rect.adjusted(8, 0, -38, 0)
        title = base_option.fontMetrics.elidedText(
            title, Qt.TextElideMode.ElideRight, max(0, text_rect.width())
        )
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter, title)
        favorite = bool(index.data(AssetItemModel.FavoriteRole))
        painter.setPen(QColor("#21a8fb") if favorite else QColor("#8a8a8a"))
        font = QFont(base_option.font)
        font.setPointSizeF(max(10.0, font.pointSizeF() + 1.0))
        painter.setFont(font)
        painter.drawText(
            self.favorite_rect(base_option.rect),
            Qt.AlignmentFlag.AlignCenter,
            "★" if favorite else "☆",
        )
        painter.restore()

class AssetTable(QTableView):
    item_selected = Signal(int)
    selection_cleared = Signal()
    item_activated = Signal(int)
    detail_requested = Signal(int)
    open_requested = Signal(int)
    favorite_requested = Signal(int, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setVerticalScrollBar(AutoHideScrollBar())
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setShowGrid(False)
        self.setAlternatingRowColors(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setWordWrap(False)
        self.setObjectName("AssetTable")
        self._asset_model = AssetItemModel(self)
        self.setModel(self._asset_model)
        self._favorite_delegate = _AssetTableFavoriteDelegate(self)
        self.setItemDelegateForColumn(0, self._favorite_delegate)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(44)
        self.horizontalHeader().setStretchLastSection(False)
        self.horizontalHeader().setFixedHeight(40)
        self.horizontalHeader().setSectionResizeMode(0, self.horizontalHeader().ResizeMode.Stretch)
        self.setColumnWidth(1, 90)
        self.setColumnWidth(2, 170)
        self.setColumnWidth(3, 160)
        self.setColumnWidth(4, 90)
        self.selectionModel().selectionChanged.connect(self._selection_changed)
        self.doubleClicked.connect(self._index_activated)
        self.selected_id: int | None = None
        self._suppress_selection_signal = False
        self._wheel_remainder = _WheelRemainder()
        self._favorite_press_row = -1
        self._right_click = _ItemRightClickGesture(self)
        self._left_click = _ItemTripleClickGesture(self)
        self._layout_updates_suspended = False
        self._content_column_resize_mode = None
        self._content_column_width = 0

    def wheelEvent(self, event) -> None:
        scaled_event = _half_speed_wheel_event(event, self._wheel_remainder)
        super().wheelEvent(scaled_event)
        event.setAccepted(scaled_event.isAccepted())

    def set_layout_updates_suspended(self, suspended: bool) -> None:
        suspended = bool(suspended)
        if suspended == self._layout_updates_suspended:
            return
        header = self.horizontalHeader()
        if suspended:
            self._layout_updates_suspended = True
            self._content_column_resize_mode = header.sectionResizeMode(0)
            self._content_column_width = self.columnWidth(0)
            header.setSectionResizeMode(0, header.ResizeMode.Fixed)
            self.setColumnWidth(0, self._content_column_width)
            return

        resize_mode = (
            self._content_column_resize_mode
            if self._content_column_resize_mode is not None
            else header.ResizeMode.Stretch
        )
        header.setSectionResizeMode(0, resize_mode)
        if resize_mode == header.ResizeMode.Fixed:
            self.setColumnWidth(0, self._content_column_width)
        self._content_column_resize_mode = None
        self._layout_updates_suspended = False
        self.viewport().update()

    def set_items(self, items, selected_id: int | None = None) -> None:
        self._right_click.cancel()
        self._left_click.cancel()
        self.selected_id = selected_id
        self._suppress_selection_signal = True
        try:
            self._asset_model.set_items(items)
            self._apply_selected_id_to_view()
        finally:
            self._suppress_selection_signal = False

    def hideEvent(self, event) -> None:
        self._right_click.cancel()
        self._left_click.cancel()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:
        self._right_click.cancel()
        self._left_click.cancel()
        super().closeEvent(event)

    def sync_selection_from_selected_id(self) -> None:
        self._suppress_selection_signal = True
        try:
            self._apply_selected_id_to_view()
        finally:
            self._suppress_selection_signal = False

    def _apply_selected_id_to_view(self) -> None:
        selected_row = self._asset_model.row_for_id(self.selected_id)
        if selected_row >= 0:
            self.selectRow(selected_row)
        else:
            self.clearSelection()
            self.setCurrentIndex(QModelIndex())

    def clear_selected_item(self) -> None:
        self.selected_id = None
        self.clearSelection()
        self.setCurrentIndex(QModelIndex())

    def clear_selection(self) -> None:
        self.clear_selected_item()

    def select_item(self, item_id: int) -> None:
        self.selected_id = item_id
        self.sync_selection_from_selected_id()
        self.item_selected.emit(item_id)

    def _selection_changed(self, _selected=None, _deselected=None) -> None:
        if self._suppress_selection_signal:
            return
        rows = self.selectionModel().selectedRows()
        if rows:
            record = self._asset_model.item(rows[0].row())
            self.selected_id = int(record["id"])
            self.item_selected.emit(self.selected_id)
        else:
            self.selected_id = None
            self.selection_cleared.emit()

    def _index_activated(self, index) -> None:
        record = self._asset_model.item(index.row())
        if record is not None:
            self.item_activated.emit(int(record["id"]))

    def mousePressEvent(self, event) -> None:
        point = event.position().toPoint()
        if event.button() == Qt.MouseButton.LeftButton:
            triple_click_id = self._triple_click_id_at(point)
            if self._left_click.press(triple_click_id):
                event.accept()
                return
        else:
            self._left_click.finish_pending()
        if event.button() == Qt.MouseButton.RightButton:
            item_id = self._item_id_at(point)
            self._right_click.press(item_id)
            if item_id is not None:
                event.accept()
                return
        else:
            self._right_click.cancel()
        index = self.indexAt(point)
        if (
            event.button() == Qt.MouseButton.LeftButton
            and index.isValid()
            and index.column() == 0
            and self._favorite_delegate.favorite_rect(self.visualRect(index)).contains(
                point
            )
        ):
            self._favorite_press_row = index.row()
            event.accept()
            return
        self._favorite_press_row = -1
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._left_click.release():
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton:
            item_id = self._item_id_at(event.position().toPoint())
            self._right_click.release(item_id)
            if item_id is not None:
                event.accept()
                return
        if self._favorite_press_row >= 0 and event.button() == Qt.MouseButton.LeftButton:
            index = self.indexAt(event.position().toPoint())
            pressed_row = self._favorite_press_row
            self._favorite_press_row = -1
            if (
                index.isValid()
                and index.row() == pressed_row
                and index.column() == 0
                and self._favorite_delegate.favorite_rect(self.visualRect(index)).contains(
                    event.position().toPoint()
                )
            ):
                record = index.data(AssetItemModel.ItemRole)
                self.select_item(int(record["id"]))
                self.favorite_requested.emit(int(record["id"]), not bool(record["favorite"]))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        point = event.position().toPoint()
        if event.button() == Qt.MouseButton.RightButton:
            event.accept()
            return
        index = self.indexAt(point)
        if (
            event.button() == Qt.MouseButton.LeftButton
            and index.isValid()
            and index.column() == 0
            and self._favorite_delegate.favorite_rect(self.visualRect(index)).contains(point)
        ):
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            triple_click_id = self._triple_click_id_at(point)
            if self._left_click.double_click(triple_click_id):
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def _item_id_at(self, point: QPoint) -> int | None:
        index = self.indexAt(point)
        if not index.isValid():
            return None
        record = index.data(AssetItemModel.ItemRole)
        return None if record is None else int(record["id"])

    def _image_id_at(self, point: QPoint) -> int | None:
        index = self.indexAt(point)
        if not index.isValid():
            return None
        record = index.data(AssetItemModel.ItemRole)
        if record is None or record["kind"] != "image":
            return None
        return int(record["id"])

    def _triple_click_id_at(self, point: QPoint) -> int | None:
        index = self.indexAt(point)
        if not index.isValid():
            return None
        record = index.data(AssetItemModel.ItemRole)
        if record is None or record["kind"] not in {"image", "text"}:
            return None
        return int(record["id"])

