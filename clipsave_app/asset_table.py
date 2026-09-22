from __future__ import annotations

from PySide6.QtCore import QModelIndex, QPoint, QRect, Qt, Signal, Slot
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QStyle,
    QStyleOptionViewItem,
    QStyledItemDelegate,
    QTableView,
)

from .item_gestures import ItemRightClickGesture, ItemTripleClickGesture
from .item_models import AssetItemModel
from .middle_autoscroll import MiddleAutoScrollController
from .ui_primitives import (
    GLYPHS,
    AutoHideScrollBar,
    WheelRemainder,
    dark_theme_active,
    half_speed_wheel_event,
    lucide_icon,
)


class _AssetTableContentDelegate(QStyledItemDelegate):
    PREVIEW_SIZE = 26

    def __init__(self, view) -> None:
        super().__init__(view)
        self.view = view
        self._icons: dict[tuple[str, bool], QPixmap] = {}

    def _icon(self, kind: str) -> QPixmap:
        dark = dark_theme_active()
        key = (kind, dark)
        pixmap = self._icons.get(key)
        if pixmap is None:
            color = "#d9e1eb" if dark else "#45576a"
            pixmap = lucide_icon(GLYPHS.get(kind, "file"), color, 18).pixmap(18, 18)
            self._icons[key] = pixmap
        return pixmap

    @staticmethod
    def _draw_background(painter, option, index) -> QStyleOptionViewItem:
        base_option = QStyleOptionViewItem(option)
        base_option.state &= ~(
            QStyle.StateFlag.State_Selected
            | QStyle.StateFlag.State_MouseOver
            | QStyle.StateFlag.State_HasFocus
        )
        base_option.text = ""
        widget = base_option.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, base_option, painter, widget)
        return base_option

    def paint(self, painter: QPainter, option, index) -> None:
        if index.column() not in (0, 1):
            clean_option = QStyleOptionViewItem(option)
            clean_option.state &= ~(
                QStyle.StateFlag.State_Selected
                | QStyle.StateFlag.State_MouseOver
                | QStyle.StateFlag.State_HasFocus
            )
            super().paint(painter, clean_option, index)
            return
        record = index.data(AssetItemModel.ItemRole)
        if record is None:
            return
        base_option = self._draw_background(painter, option, index)
        painter.save()
        text_color = base_option.palette.text().color()
        if index.column() == 0:
            preview = QRect(
                base_option.rect.left() + 10,
                base_option.rect.center().y() - self.PREVIEW_SIZE // 2,
                self.PREVIEW_SIZE,
                self.PREVIEW_SIZE,
            )
            if record["kind"] == "image" and record["path"]:
                try:
                    content_hash = record["content_hash"]
                except (KeyError, IndexError):
                    content_hash = None
                pixmap = self.view.thumbnail_for_index(index, record["path"], content_hash)
                if pixmap is not None and not pixmap.isNull():
                    scaled = pixmap.scaled(
                        preview.size(),
                        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    target = QRect(0, 0, scaled.width(), scaled.height())
                    target.moveCenter(preview.center())
                    clip = QPainterPath()
                    clip.addRoundedRect(preview, 4, 4)
                    painter.save()
                    painter.setClipPath(clip)
                    painter.drawPixmap(target, scaled)
                    painter.restore()
                else:
                    painter.drawPixmap(preview.center() - self._icon("image").rect().center(), self._icon("image"))
            else:
                icon = self._icon(str(record["kind"]))
                painter.drawPixmap(preview.center() - icon.rect().center(), icon)
            text_rect = QRect(
                preview.right() + 10,
                base_option.rect.top(),
                max(0, base_option.rect.right() - preview.right() - 18),
                base_option.rect.height(),
            )
            title = base_option.fontMetrics.elidedText(
                str(record["title"]), Qt.TextElideMode.ElideRight, text_rect.width()
            )
            painter.setPen(text_color)
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter, title)
        else:
            self._draw_tags(painter, base_option, record, text_color)
        painter.restore()

    @staticmethod
    def _draw_tags(painter, option, record, text_color: QColor) -> None:
        names = [name for name in str(record["tag_names"] or "").split("\x1f") if name]
        if not names:
            return
        dark = dark_theme_active()
        background = QColor(255, 255, 255, 18) if dark else QColor(39, 75, 125, 16)
        x = option.rect.left() + 8
        right = option.rect.right() - 8
        visible = names[:2]
        if len(names) > 2:
            visible.append(f"+{len(names) - 2}")
        painter.setPen(Qt.PenStyle.NoPen)
        for position, name in enumerate(visible):
            remaining = right - x
            if remaining < 24:
                break
            label = option.fontMetrics.elidedText(
                name, Qt.TextElideMode.ElideRight, min(90, max(12, remaining - 14))
            )
            width = min(remaining, option.fontMetrics.horizontalAdvance(label) + 14)
            chip = QRect(x, option.rect.center().y() - 10, width, 20)
            painter.setBrush(background)
            painter.drawRoundedRect(chip, 5, 5)
            painter.setPen(text_color)
            painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, label)
            painter.setPen(Qt.PenStyle.NoPen)
            x = chip.right() + 6

class AssetTable(QTableView):
    item_selected = Signal(int)
    selection_cleared = Signal()
    item_activated = Signal(int)
    detail_requested = Signal(int)
    open_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setVerticalScrollBar(
            AutoHideScrollBar(
                track_width=10,
                dark_background="#202020",
                always_visible=True,
            )
        )
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setShowGrid(False)
        self.setAlternatingRowColors(False)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setWordWrap(False)
        self.setObjectName("AssetTable")
        self._asset_model = AssetItemModel(self)
        self.setModel(self._asset_model)
        self._content_delegate = _AssetTableContentDelegate(self)
        self.setItemDelegate(self._content_delegate)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(40)
        self.horizontalHeader().hide()
        self.horizontalHeader().setStretchLastSection(False)
        self.horizontalHeader().setFixedHeight(34)
        self.horizontalHeader().setObjectName("AssetTableHeader")
        self.horizontalHeader().setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.horizontalHeader().setAutoFillBackground(False)
        self.horizontalHeader().viewport().setObjectName("AssetTableHeaderViewport")
        self.horizontalHeader().viewport().setAttribute(
            Qt.WidgetAttribute.WA_StyledBackground, True
        )
        self.horizontalHeader().viewport().setAutoFillBackground(False)
        self.horizontalHeader().setSectionResizeMode(0, self.horizontalHeader().ResizeMode.Stretch)
        self.setColumnWidth(1, 190)
        self.setColumnWidth(2, 135)
        self.setColumnWidth(3, 90)
        self.selectionModel().selectionChanged.connect(self._selection_changed)
        self.doubleClicked.connect(self._index_activated)
        self.selected_id: int | None = None
        self._suppress_selection_signal = False
        self._wheel_remainder = WheelRemainder()
        self._thumbnail_provider = None
        self._right_click = ItemRightClickGesture(self)
        self._left_click = ItemTripleClickGesture(self)
        self._layout_updates_suspended = False
        self._content_column_resize_mode = None
        self._content_column_width = 0
        self._middle_autoscroll = MiddleAutoScrollController(self)

    def set_thumbnail_provider(self, provider) -> None:
        old = self._thumbnail_provider
        if old is provider:
            return
        if old is not None:
            try:
                old.thumbnail_available.disconnect(self._thumbnail_available)
            except (RuntimeError, TypeError):
                pass
        self._thumbnail_provider = provider
        if provider is not None:
            provider.thumbnail_available.connect(self._thumbnail_available)

    def thumbnail_for_index(self, _index, path, content_hash=None):
        if self._thumbnail_provider is None:
            return None
        return self._thumbnail_provider.thumbnail_for_external_view(path, content_hash)

    @Slot(str)
    def _thumbnail_available(self, _path: str) -> None:
        if self.isVisible():
            self.viewport().update()

    def wheelEvent(self, event) -> None:
        scaled_event = half_speed_wheel_event(event, self._wheel_remainder)
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
        self._middle_autoscroll.cancel()
        self._right_click.cancel()
        self._left_click.cancel()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:
        self._middle_autoscroll.cancel()
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
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        point = event.position().toPoint()
        if event.button() == Qt.MouseButton.RightButton:
            event.accept()
            return
        index = self.indexAt(point)
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
