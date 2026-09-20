from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (
    QItemSelectionModel,
    QModelIndex,
    QPoint,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QColor,
    QImage,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QListView,
)

from .asset_grid_delegate import AssetGridDelegate
from .asset_grid_transition import (
    _AssetGridTransitionOverlay,
    _GridTransitionCard,
    _grid_transition_card_elevated,
)
from .item_gestures import _ItemRightClickGesture, _ItemTripleClickGesture
from .item_models import AssetItemModel
from .sidebar import Sidebar
from .thumbnail_service import (
    ThumbnailDecodeQueue,
    _ThumbnailCacheKey,
    _cache_decoded_thumbnail,
    _cached_thumbnail,
)
from .ui_primitives import (
    AutoHideScrollBar,
    _WheelRemainder,
    _half_speed_wheel_event,
    dark_theme_active,
)

class AssetGrid(QListView):
    item_selected = Signal(int)
    selection_cleared = Signal()
    item_activated = Signal(int)
    detail_requested = Signal(int)

    def _make_thumbnail_queue(self, parent) -> ThumbnailDecodeQueue:
        return ThumbnailDecodeQueue(parent)

    def _thumbnail_lookup(self, path, content_hash=None):
        return _cached_thumbnail(path, content_hash)

    def _store_decoded_thumbnail(self, key, image):
        return _cache_decoded_thumbnail(key, image)
    open_requested = Signal(int)
    favorite_requested = Signal(int, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBar(AutoHideScrollBar())
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setViewportMargins(14, 48, 14, 18)
        self.setSpacing(0)
        self.setObjectName("AssetGrid")
        self._sidebar_transition_active = False
        self._sidebar_transition_overlay: _AssetGridTransitionOverlay | None = None
        self._pending_items_update: tuple[list, int | None] | None = None
        self._asset_model = AssetItemModel(self)
        self.setModel(self._asset_model)
        self.delegate = AssetGridDelegate(self)
        self.setItemDelegate(self.delegate)
        self.items = self._asset_model.items
        self.selected_id: int | None = None
        self.columns = 0
        self.preview_loading_enabled = True
        self.rebuild_pending = False
        self._layout_updates_suspended = False
        self._layout_update_pending = False
        self._layout_resize_mode = None
        self._thumbnail_generation = 0
        self._thumbnail_loader = self._make_thumbnail_queue(self)
        self._thumbnail_loader.decoded.connect(self._thumbnail_decoded)
        self._thumbnail_loader.capacity_available.connect(
            self._thumbnail_capacity_available
        )
        self._thumbnail_refresh_timer = QTimer(self)
        self._thumbnail_refresh_timer.setSingleShot(True)
        self._thumbnail_refresh_timer.setInterval(50)
        self._thumbnail_refresh_timer.timeout.connect(self._refresh_thumbnail_generation)
        self._favorite_press_row = -1
        self._suppress_selection_signal = False
        self._wheel_remainder = _WheelRemainder()
        self._right_click = _ItemRightClickGesture(self)
        self._left_click = _ItemTripleClickGesture(self)
        self.selectionModel().currentChanged.connect(self._index_selected)
        self.selectionModel().selectionChanged.connect(self._selection_changed)
        self.doubleClicked.connect(self._index_activated)
        self.verticalScrollBar().valueChanged.connect(self._thumbnail_viewport_changed)
        self._update_grid_size()

    def set_items(self, items, selected_id: int | None = None) -> None:
        if self._sidebar_transition_active:
            self._pending_items_update = (list(items), selected_id)
            return
        self._apply_items_now(items, selected_id)

    def _apply_items_now(self, items, selected_id: int | None = None) -> None:
        self._clear_sidebar_transition(repaint=False)
        self._right_click.cancel()
        self._left_click.cancel()
        self._thumbnail_refresh_timer.stop()
        self._thumbnail_generation += 1
        self._thumbnail_loader.cancel_queued()
        self.selected_id = selected_id
        self._asset_model.set_items(items)
        self.items = self._asset_model.items
        self.rebuild_pending = not self.isVisible()
        self._restore_selection()
        self.viewport().update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._layout_updates_suspended:
            self._layout_update_pending = True
            return
        self._update_grid_size()
        self._thumbnail_refresh_timer.start()

    def wheelEvent(self, event) -> None:
        scaled_event = _half_speed_wheel_event(event, self._wheel_remainder)
        super().wheelEvent(scaled_event)
        event.setAccepted(scaled_event.isAccepted())

    def set_layout_updates_suspended(self, suspended: bool) -> None:
        suspended = bool(suspended)
        if suspended == self._layout_updates_suspended:
            return
        if suspended:
            self._layout_updates_suspended = True
            self._layout_update_pending = False
            self._layout_resize_mode = self.resizeMode()
            self._thumbnail_refresh_timer.stop()
            self._thumbnail_generation += 1
            self._thumbnail_loader.cancel_queued()
            self.setResizeMode(QListView.ResizeMode.Fixed)
            return

        self._thumbnail_refresh_timer.stop()
        self._thumbnail_generation += 1
        self._thumbnail_loader.cancel_queued()
        resize_mode = (
            self._layout_resize_mode
            if self._layout_resize_mode is not None
            else QListView.ResizeMode.Adjust
        )
        self.setResizeMode(resize_mode)
        self._layout_resize_mode = None
        self._layout_update_pending = False
        self._update_grid_size()
        self._layout_updates_suspended = False
        self.viewport().update()

    @staticmethod
    def _layout_for_viewport_width(width: int) -> tuple[int, QSize]:
        available = max(210, int(width))
        columns = max(1, available // 245)
        gap = 12
        layout_width = max(210, available - 1)
        card_width = max(210, (layout_width - columns * gap) // columns)
        return columns, QSize(
            card_width + gap,
            AssetGridDelegate.card_height + gap,
        )

    def _update_grid_size(self) -> None:
        available = max(210, self.viewport().width())
        columns, grid_size = self._layout_for_viewport_width(available)
        self.columns = columns
        self.setGridSize(grid_size)

    @staticmethod
    def _transition_cell_rect(
        row: int,
        columns: int,
        grid_size: QSize,
        scroll_offset: int,
    ) -> QRectF:
        layout_row, column = divmod(row, columns)
        return QRectF(
            column * grid_size.width(),
            layout_row * grid_size.height() - scroll_offset,
            grid_size.width(),
            grid_size.height(),
        )

    def _transition_rows(
        self,
        columns: int,
        grid_size: QSize,
        scroll_offset: int,
    ) -> range:
        count = self._asset_model.rowCount()
        if count <= 0:
            return range(0)
        first_layout_row = max(0, scroll_offset // grid_size.height() - 1)
        last_layout_row = (
            scroll_offset + self.viewport().height()
        ) // grid_size.height() + 1
        return range(
            min(count, first_layout_row * columns),
            min(count, (last_layout_row + 1) * columns),
        )

    def begin_sidebar_transition(
        self,
        current_sidebar_width: int,
        progress: float,
        expanded_sidebar_width: int = Sidebar.EXPANDED_WIDTH,
        collapsed_sidebar_width: int = Sidebar.COLLAPSED_WIDTH,
    ) -> bool:
        self._clear_sidebar_transition(repaint=False)
        if (
            not self.isVisible()
            or self._asset_model.rowCount() <= 0
            or self.viewport().width() <= 0
            or self.viewport().height() <= 0
        ):
            return False

        current_sidebar_width = max(
            collapsed_sidebar_width,
            min(expanded_sidebar_width, int(current_sidebar_width)),
        )
        current_width = self.viewport().width()
        expanded_width = max(
            1,
            current_width
            - (expanded_sidebar_width - current_sidebar_width),
        )
        collapsed_width = max(
            1,
            current_width
            + (current_sidebar_width - collapsed_sidebar_width),
        )
        return self.begin_viewport_width_transition(
            expanded_width,
            collapsed_width,
            progress,
        )

    def begin_viewport_width_transition(
        self,
        start_viewport_width: int,
        end_viewport_width: int,
        progress: float = 0.0,
    ) -> bool:
        self._clear_sidebar_transition(repaint=False)
        if (
            not self.isVisible()
            or self._asset_model.rowCount() <= 0
            or self.viewport().width() <= 0
            or self.viewport().height() <= 0
        ):
            return False
        start_columns, start_size = self._layout_for_viewport_width(
            max(1, int(start_viewport_width))
        )
        end_columns, end_size = self._layout_for_viewport_width(
            max(1, int(end_viewport_width))
        )
        scroll_offset = self.verticalScrollBar().value()
        rows = sorted(
            set(
                self._transition_rows(
                    start_columns,
                    start_size,
                    scroll_offset,
                )
            )
            | set(
                self._transition_rows(
                    end_columns,
                    end_size,
                    scroll_offset,
                )
            )
        )
        if not rows:
            return False

        cards = [
            _GridTransitionCard(
                row=row,
                expanded_rect=self._transition_cell_rect(
                    row,
                    start_columns,
                    start_size,
                    scroll_offset,
                ),
                collapsed_rect=self._transition_cell_rect(
                    row,
                    end_columns,
                    end_size,
                    scroll_offset,
                ),
                elevated=_grid_transition_card_elevated(
                    row,
                    start_columns,
                    end_columns,
                ),
            )
            for row in rows
        ]
        overlay = _AssetGridTransitionOverlay(
            self,
            cards,
            start_columns,
            end_columns,
            progress,
        )
        self._sidebar_transition_overlay = overlay
        self._sidebar_transition_active = True
        return True

    def set_sidebar_transition_progress(self, progress: float) -> None:
        overlay = self._sidebar_transition_overlay
        if not self._sidebar_transition_active or overlay is None:
            return
        dirty = overlay.set_progress(progress)
        if not dirty.isEmpty():
            self.viewport().update(dirty)

    def finish_sidebar_transition(self) -> None:
        self._clear_sidebar_transition(repaint=True)
        self._apply_pending_items_update()

    def _apply_pending_items_update(self) -> None:
        pending = self._pending_items_update
        self._pending_items_update = None
        if pending is not None:
            self._apply_items_now(*pending)

    def _clear_sidebar_transition(self, repaint: bool) -> None:
        overlay = self._sidebar_transition_overlay
        if overlay is None and not self._sidebar_transition_active:
            return
        self._sidebar_transition_active = False
        self._sidebar_transition_overlay = None
        if repaint and self.viewport().isVisible():
            self.viewport().repaint()

    def set_preview_loading_enabled(self, enabled: bool) -> None:
        if enabled == self.preview_loading_enabled:
            return
        self._thumbnail_generation += 1
        self._thumbnail_loader.cancel_queued()
        self.preview_loading_enabled = enabled
        if enabled and self.isVisible() and not self._layout_updates_suspended:
            self.viewport().update()

    def hideEvent(self, event) -> None:
        self._clear_sidebar_transition(repaint=False)
        self.delegate.clear_transition_caches()
        self._apply_pending_items_update()
        self._right_click.cancel()
        self._left_click.cancel()
        self._thumbnail_refresh_timer.stop()
        self._thumbnail_generation += 1
        self._thumbnail_loader.cancel_queued()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.rebuild_pending:
            self.rebuild_pending = False
            if self._layout_updates_suspended:
                self._layout_update_pending = True
            else:
                self._update_grid_size()
        if self.preview_loading_enabled and not self._layout_updates_suspended:
            self.viewport().update()

    def closeEvent(self, event) -> None:
        self._clear_sidebar_transition(repaint=False)
        self._apply_pending_items_update()
        self._right_click.cancel()
        self._left_click.cancel()
        self._thumbnail_refresh_timer.stop()
        self._thumbnail_generation += 1
        self._thumbnail_loader.close()
        super().closeEvent(event)

    def shutdown_thumbnail_loader(self, timeout_ms: int = 2000) -> bool:
        self._thumbnail_generation += 1
        return self._thumbnail_loader.close(timeout_ms)

    def wait_for_thumbnail_idle(self) -> bool:
        self._thumbnail_refresh_timer.stop()
        self._thumbnail_generation += 1
        return self._thumbnail_loader.pause_and_wait()

    def resume_thumbnail_loader(self) -> None:
        self._thumbnail_loader.resume()
        if (
            self.preview_loading_enabled
            and self.isVisible()
            and not self._layout_updates_suspended
        ):
            self.viewport().update()

    def thumbnail_for_index(
        self, index: QModelIndex, path: Path | str, content_hash: str | None = None
    ) -> QPixmap | None:
        key, pixmap, cached = self._thumbnail_lookup(path, content_hash)
        if cached:
            return pixmap
        if (
            key is None
            or not self.preview_loading_enabled
            or self._layout_updates_suspended
            or not self.isVisible()
            or not index.isValid()
            or not self.visualRect(index).intersects(self.viewport().rect())
        ):
            return None
        self._thumbnail_loader.request(key, self._thumbnail_generation)
        return None

    @Slot(object, object, int)
    def _thumbnail_decoded(self, key: _ThumbnailCacheKey, image: QImage, generation: int) -> None:
        if generation != self._thumbnail_generation:
            return
        model_generation = self._asset_model.generation
        if not self._asset_model.has_thumbnail_path(key.path, model_generation):
            return
        if self._store_decoded_thumbnail(key, image) is None:
            return
        self._asset_model.notify_thumbnail_changed(key.path, model_generation)

    def _thumbnail_viewport_changed(self, _value: int) -> None:
        if self._layout_updates_suspended:
            return
        self._thumbnail_refresh_timer.start()

    def _refresh_thumbnail_generation(self) -> None:
        if self._layout_updates_suspended:
            return
        self._thumbnail_generation += 1
        self._thumbnail_loader.cancel_queued()
        self.viewport().update()

    def _thumbnail_capacity_available(self) -> None:
        if not self._layout_updates_suspended:
            self.viewport().update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self.viewport())
        overlay = self._sidebar_transition_overlay
        if self._sidebar_transition_active and overlay is not None:
            overlay.paint(painter, self.viewport().rect())
        elif self._asset_model.rowCount() == 0:
            painter.setPen(QColor("#a7adb7" if dark_theme_active() else "#7a8699"))
            painter.drawText(self.viewport().rect(), Qt.AlignmentFlag.AlignCenter, "没有找到符合条件的内容")
        painter.end()

    def clear_selection(self) -> None:
        self.selected_id = None
        self.clearSelection()
        self.setCurrentIndex(QModelIndex())

    def clear_selected_item(self) -> None:
        self.clear_selection()

    def select_item(self, item_id: int) -> None:
        self.selected_id = item_id
        self.sync_selection_from_selected_id()
        self.item_selected.emit(item_id)

    def sync_selection_from_selected_id(self) -> None:
        row = self._asset_model.row_for_id(self.selected_id)
        self._suppress_selection_signal = True
        try:
            if row < 0:
                self.clearSelection()
                self.setCurrentIndex(QModelIndex())
                return
            index = self._asset_model.index(row, 0)
            self.selectionModel().select(
                index,
                QItemSelectionModel.SelectionFlag.ClearAndSelect,
            )
            self.setCurrentIndex(index)
        finally:
            self._suppress_selection_signal = False

    def _restore_selection(self) -> None:
        self.sync_selection_from_selected_id()

    def _index_selected(self, index, _previous=QModelIndex()) -> None:
        if self._suppress_selection_signal:
            return
        record = index.data(AssetItemModel.ItemRole)
        if record is None:
            return
        self.selected_id = int(record["id"])
        self.item_selected.emit(self.selected_id)

    def _selection_changed(self, _selected=None, _deselected=None) -> None:
        if self._suppress_selection_signal or self.selectionModel().selectedIndexes():
            return
        self.selected_id = None
        self.selection_cleared.emit()

    def _index_activated(self, index) -> None:
        record = index.data(AssetItemModel.ItemRole)
        if record is not None:
            self.item_activated.emit(int(record["id"]))

    def _visual_index_at(self, point: QPoint) -> QModelIndex:
        overlay = self._sidebar_transition_overlay
        if self._sidebar_transition_active and overlay is not None:
            return overlay.index_at(point)
        return self.indexAt(point)

    def _visual_rect_for_index(self, index: QModelIndex) -> QRect:
        overlay = self._sidebar_transition_overlay
        if (
            self._sidebar_transition_active
            and overlay is not None
            and index.isValid()
        ):
            rect = overlay.card_rect(index.row())
            if rect is not None:
                return rect.toRect()
        return self.visualRect(index)

    def _favorite_rect_for_index(self, index: QModelIndex) -> QRect:
        return self.delegate.favorite_rect(self._visual_rect_for_index(index))

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
        if event.button() == Qt.MouseButton.LeftButton:
            index = self._visual_index_at(point)
            if index.isValid() and self._favorite_rect_for_index(index).contains(point):
                self._favorite_press_row = index.row()
                event.accept()
                return
            if self._sidebar_transition_active and index.isValid():
                self.selectionModel().select(
                    index,
                    QItemSelectionModel.SelectionFlag.ClearAndSelect,
                )
                self.setCurrentIndex(index)
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
            index = self._visual_index_at(event.position().toPoint())
            pressed_row = self._favorite_press_row
            self._favorite_press_row = -1
            if (
                index.isValid()
                and index.row() == pressed_row
                and self._favorite_rect_for_index(index).contains(
                    event.position().toPoint()
                )
            ):
                record = index.data(AssetItemModel.ItemRole)
                self.select_item(int(record["id"]))
                self.favorite_requested.emit(int(record["id"]), not bool(record["favorite"]))
            event.accept()
            return
        if (
            self._sidebar_transition_active
            and event.button() == Qt.MouseButton.LeftButton
        ):
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        point = event.position().toPoint()
        if event.button() == Qt.MouseButton.RightButton:
            event.accept()
            return
        index = self._visual_index_at(point)
        if (
            event.button() == Qt.MouseButton.LeftButton
            and index.isValid()
            and self._favorite_rect_for_index(index).contains(point)
        ):
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            triple_click_id = self._triple_click_id_at(point)
            if self._left_click.double_click(triple_click_id):
                event.accept()
                return
            if self._sidebar_transition_active and index.isValid():
                self._index_activated(index)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def _item_id_at(self, point: QPoint) -> int | None:
        index = self._visual_index_at(point)
        if not index.isValid():
            return None
        record = index.data(AssetItemModel.ItemRole)
        return None if record is None else int(record["id"])

    def _image_id_at(self, point: QPoint) -> int | None:
        index = self._visual_index_at(point)
        if not index.isValid():
            return None
        record = index.data(AssetItemModel.ItemRole)
        if record is None or record["kind"] != "image":
            return None
        return int(record["id"])

    def _triple_click_id_at(self, point: QPoint) -> int | None:
        index = self._visual_index_at(point)
        if not index.isValid():
            return None
        record = index.data(AssetItemModel.ItemRole)
        if record is None or record["kind"] not in {"image", "text"}:
            return None
        return int(record["id"])

