from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import math
import time

from PySide6.QtCore import (
    QItemSelectionModel,
    QModelIndex,
    QPoint,
    QPointF,
    QRect,
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
    QStyle,
    QStyleOptionViewItem,
)

from .asset_grid_delegate import AssetGridDelegate
from .asset_grid_transition import (
    AssetGridTransitionOverlay,
)
from .asset_grid_transition_controller import AssetGridTransitionController
from .item_gestures import ItemRightClickGesture, ItemTripleClickGesture
from .item_models import AssetItemModel
from .middle_autoscroll import MiddleAutoScrollController
from .sidebar import Sidebar
from .thumbnail_service import (
    ThumbnailDecodeQueue,
    ThumbnailCacheKey,
    cache_decoded_thumbnail,
    cached_thumbnail,
)
from .thumbnail_session import ThumbnailSession
from .ui_primitives import (
    AutoHideScrollBar,
    WheelRemainder,
    half_speed_wheel_event,
    dark_theme_active,
)

@dataclass(slots=True)
class PaperPeelState:
    row: int
    current: QPointF
    press: QPointF
    animation_start: QPointF | None = None
    animation_end: QPointF | None = None
    animation_started: float = 0.0
    animation_duration: float = 0.0
    animation_curved: bool = False
    commit: bool = False
    waiting_for_result: bool = False


class AssetGrid(QListView):
    item_selected = Signal(int)
    selection_cleared = Signal()
    item_activated = Signal(int)
    detail_requested = Signal(int)
    thumbnail_available = Signal(str)

    def _make_thumbnail_queue(self, parent) -> ThumbnailDecodeQueue:
        return ThumbnailDecodeQueue(parent)

    def _thumbnail_lookup(self, path, content_hash=None):
        return cached_thumbnail(path, content_hash)

    def _store_decoded_thumbnail(self, key, image):
        return cache_decoded_thumbnail(key, image)
    open_requested = Signal(int)
    favorite_requested = Signal(int, bool)

    @property
    def _thumbnail_generation(self) -> int:
        return self._thumbnail_session.generation

    @property
    def _sidebar_transition_active(self) -> bool:
        return self._transition_controller.active

    @property
    def _sidebar_transition_overlay(self) -> AssetGridTransitionOverlay | None:
        return self._transition_controller.overlay

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
        self.setVerticalScrollBar(
            AutoHideScrollBar(
                track_width=10,
                dark_background="#202020",
                always_visible=True,
            )
        )
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setViewportMargins(14, 48, 14, 18)
        self.setSpacing(0)
        self.setObjectName("AssetGrid")
        self._transition_controller = AssetGridTransitionController(self)
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
        self._thumbnail_loader = self._make_thumbnail_queue(self)
        self._thumbnail_session = ThumbnailSession(self._thumbnail_loader)
        self._thumbnail_loader.decoded.connect(self._thumbnail_decoded)
        self._thumbnail_loader.capacity_available.connect(
            self._thumbnail_capacity_available
        )
        self._thumbnail_refresh_timer = QTimer(self)
        self._thumbnail_refresh_timer.setSingleShot(True)
        self._thumbnail_refresh_timer.setInterval(50)
        self._thumbnail_refresh_timer.timeout.connect(self._refresh_thumbnail_generation)
        self._favorite_press_row = -1
        self._paper_peel: PaperPeelState | None = None
        self._favorite_page_mode = False
        self._paper_animation = QTimer(self)
        self._paper_animation.setInterval(16)
        self._paper_animation.timeout.connect(self._advance_paper_animation)
        self._suppress_selection_signal = False
        self._wheel_remainder = WheelRemainder()
        self._right_click = ItemRightClickGesture(self)
        self._left_click = ItemTripleClickGesture(self)
        self.selectionModel().currentChanged.connect(self._index_selected)
        self.selectionModel().selectionChanged.connect(self._selection_changed)
        self.doubleClicked.connect(self._index_activated)
        self.verticalScrollBar().valueChanged.connect(self._thumbnail_viewport_changed)
        self.verticalScrollBar().valueChanged.connect(lambda _value: self.cancel_paper_peel())
        self._middle_autoscroll = MiddleAutoScrollController(self)
        self._update_grid_size()

    @property
    def favorite_page_mode(self) -> bool:
        """Whether cards are single sheets that leave the filtered page when peeled."""
        return self._favorite_page_mode

    def set_favorite_page_mode(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._favorite_page_mode:
            return
        self.cancel_paper_peel()
        self._favorite_page_mode = enabled
        self.viewport().update()

    def set_items(self, items, selected_id: int | None = None) -> None:
        if self._sidebar_transition_active:
            self._pending_items_update = (list(items), selected_id)
            return
        self._apply_items_now(items, selected_id)

    def _apply_items_now(self, items, selected_id: int | None = None) -> None:
        settle_item_id = None
        state = self._paper_peel
        if state is not None and state.waiting_for_result:
            old_index = self.model().index(state.row, 0)
            old_record = old_index.data(AssetItemModel.ItemRole) if old_index.isValid() else None
            if old_record is not None:
                settle_item_id = int(old_record["id"])
        self.cancel_paper_peel()
        self._clear_sidebar_transition(repaint=False)
        self._right_click.cancel()
        self._left_click.cancel()
        self._thumbnail_refresh_timer.stop()
        self._thumbnail_session.invalidate()
        self.selected_id = selected_id
        self._asset_model.set_items(items)
        self.items = self._asset_model.items
        self.rebuild_pending = not self.isVisible()
        self._restore_selection()
        if settle_item_id is not None:
            self._settle_new_paper_corner(settle_item_id)
        self.viewport().update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._layout_updates_suspended:
            self._layout_update_pending = True
            return
        self._update_grid_size()
        self._thumbnail_refresh_timer.start()

    def wheelEvent(self, event) -> None:
        scaled_event = half_speed_wheel_event(event, self._wheel_remainder)
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
            self._thumbnail_session.invalidate()
            self.setResizeMode(QListView.ResizeMode.Fixed)
            return

        self._thumbnail_refresh_timer.stop()
        self._thumbnail_session.invalidate()
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
        return AssetGridTransitionController.layout_for_viewport_width(width)

    def _update_grid_size(self) -> None:
        available = max(210, self.viewport().width())
        columns, grid_size = self._layout_for_viewport_width(available)
        self.columns = columns
        self.setGridSize(grid_size)

    def begin_sidebar_transition(
        self,
        current_sidebar_width: int,
        progress: float,
        expanded_sidebar_width: int = Sidebar.EXPANDED_WIDTH,
        collapsed_sidebar_width: int = Sidebar.COLLAPSED_WIDTH,
    ) -> bool:
        self.cancel_paper_peel()
        return self._transition_controller.begin_sidebar(
            current_sidebar_width,
            progress,
            expanded_sidebar_width,
            collapsed_sidebar_width,
        )

    def begin_viewport_width_transition(
        self,
        start_viewport_width: int,
        end_viewport_width: int,
        progress: float = 0.0,
    ) -> bool:
        self.cancel_paper_peel()
        return self._transition_controller.begin_viewport(
            start_viewport_width,
            end_viewport_width,
            progress,
        )

    def set_sidebar_transition_progress(self, progress: float) -> None:
        self._transition_controller.set_progress(progress)

    def finish_sidebar_transition(self) -> None:
        self._clear_sidebar_transition(repaint=True)
        self._apply_pending_items_update()

    def _apply_pending_items_update(self) -> None:
        pending = self._pending_items_update
        self._pending_items_update = None
        if pending is not None:
            self._apply_items_now(*pending)

    def _clear_sidebar_transition(self, repaint: bool) -> None:
        self._transition_controller.clear(repaint)

    def set_preview_loading_enabled(self, enabled: bool) -> None:
        if enabled == self.preview_loading_enabled:
            return
        self._thumbnail_session.invalidate()
        self.preview_loading_enabled = enabled
        if enabled and self.isVisible() and not self._layout_updates_suspended:
            self.viewport().update()

    def hideEvent(self, event) -> None:
        self.cancel_paper_peel()
        self._middle_autoscroll.cancel()
        self._clear_sidebar_transition(repaint=False)
        self.delegate.clear_transition_caches()
        self._apply_pending_items_update()
        self._right_click.cancel()
        self._left_click.cancel()
        self._thumbnail_refresh_timer.stop()
        self._thumbnail_session.invalidate()
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
        self.cancel_paper_peel()
        self._middle_autoscroll.cancel()
        self._clear_sidebar_transition(repaint=False)
        self._apply_pending_items_update()
        self._right_click.cancel()
        self._left_click.cancel()
        self._thumbnail_refresh_timer.stop()
        self._thumbnail_session.close()
        super().closeEvent(event)

    def shutdown_thumbnail_loader(self, timeout_ms: int = 2000) -> bool:
        return self._thumbnail_session.close(timeout_ms)

    def wait_for_thumbnail_idle(self) -> bool:
        self._thumbnail_refresh_timer.stop()
        return self._thumbnail_session.pause_and_wait()

    def resume_thumbnail_loader(self) -> None:
        self._thumbnail_session.resume()
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
        self._thumbnail_session.request(key)
        return None

    def thumbnail_for_external_view(
        self, path: Path | str, content_hash: str | None = None
    ) -> QPixmap | None:
        key, pixmap, cached = self._thumbnail_lookup(path, content_hash)
        if cached:
            return pixmap
        if key is not None:
            self._thumbnail_session.request(key)
        return None

    @Slot(object, object, int)
    def _thumbnail_decoded(self, key: ThumbnailCacheKey, image: QImage, generation: int) -> None:
        if not self._thumbnail_session.is_current(generation):
            return
        if self._store_decoded_thumbnail(key, image) is None:
            return
        model_generation = self._asset_model.generation
        self._asset_model.notify_thumbnail_changed(key.path, model_generation)
        self.thumbnail_available.emit(key.path)

    def _thumbnail_viewport_changed(self, _value: int) -> None:
        if self._layout_updates_suspended:
            return
        self._thumbnail_refresh_timer.start()

    def _refresh_thumbnail_generation(self) -> None:
        if self._layout_updates_suspended:
            return
        self._thumbnail_session.invalidate()
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
        else:
            self._paint_active_paper(painter)
        painter.end()

    def _paint_active_paper(self, painter: QPainter) -> None:
        state = self._paper_peel
        if state is None:
            return
        index = self.model().index(state.row, 0)
        if not index.isValid():
            return
        rect = self.visualRect(index)
        if rect.isEmpty():
            return
        option = QStyleOptionViewItem()
        self.initViewItemOption(option)
        option.rect = rect
        option.widget = self
        option.state |= QStyle.StateFlag.State_Active | QStyle.StateFlag.State_Enabled
        if self.selectionModel().isSelected(index):
            option.state |= QStyle.StateFlag.State_Selected
        else:
            option.state &= ~QStyle.StateFlag.State_Selected
        self.delegate.paint_transition_card(painter, option, index)

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

    def paper_peel_state(self, row: int) -> PaperPeelState | None:
        state = self._paper_peel
        return state if state is not None and state.row == row else None

    def cancel_paper_peel(self) -> None:
        state = self._paper_peel
        self._paper_animation.stop()
        self._paper_peel = None
        self._favorite_press_row = -1
        if state is not None:
            self.viewport().update()

    def _start_paper_peel(self, index: QModelIndex, point: QPoint) -> None:
        cell = self._visual_rect_for_index(index)
        local = QPointF(point - cell.topLeft())
        record = index.data(AssetItemModel.ItemRole)
        if record is not None:
            self.select_item(int(record["id"]))
        self._paper_animation.stop()
        self._paper_peel = PaperPeelState(index.row(), local, local)
        self._favorite_press_row = index.row()
        self.viewport().update()

    def _bounded_paper_point(self, index: QModelIndex, point: QPoint) -> QPointF:
        cell = self._visual_rect_for_index(index)
        local = QPointF(point - cell.topLeft())
        card = self.delegate.card_rect(QRect(0, 0, cell.width(), cell.height()))
        corner = self.delegate.paper_corner(card)
        vector = local - corner
        distance = math.hypot(vector.x(), vector.y())
        maximum = max(1.0, math.hypot(card.width(), card.height()) * 1.65)
        if distance > maximum:
            vector *= maximum / distance
            local = corner + vector
        return local

    def _animate_paper_peel(
        self,
        end: QPointF,
        duration_ms: int,
        commit: bool,
        *,
        curved: bool = False,
    ) -> None:
        state = self._paper_peel
        if state is None:
            return
        state.animation_start = QPointF(state.current)
        state.animation_end = QPointF(end)
        state.animation_started = time.monotonic()
        state.animation_duration = max(0.001, duration_ms / 1000.0)
        state.animation_curved = curved
        state.commit = commit
        self._paper_animation.start()

    def _settle_new_paper_corner(self, item_id: int) -> None:
        row = self._asset_model.row_for_id(item_id)
        if row < 0:
            return
        index = self.model().index(row, 0)
        cell = self.visualRect(index)
        if cell.isEmpty():
            return
        card = self.delegate.card_rect(QRect(0, 0, cell.width(), cell.height()))
        corner = self.delegate.paper_corner(card)
        flat = corner + QPointF(-1.0, 1.0)
        idle = corner + QPointF(-self.delegate.CORNER_SIZE, self.delegate.CORNER_SIZE)
        self._paper_peel = PaperPeelState(row, flat, flat)
        self._animate_paper_peel(idle, 120, False)

    def _advance_paper_animation(self) -> None:
        state = self._paper_peel
        if state is None or state.animation_start is None or state.animation_end is None:
            self._paper_animation.stop()
            return
        progress = min(1.0, (time.monotonic() - state.animation_started) / state.animation_duration)
        eased = (
            progress * progress * (3.0 - 2.0 * progress)
            if state.animation_curved
            else 1.0 - (1.0 - progress) ** 3
        )
        if state.animation_curved:
            start, end = state.animation_start, state.animation_end
            span = end - start
            control1 = start + QPointF(span.x() * 0.18, span.y() * 0.04)
            control2 = start + QPointF(span.x() * 0.72, span.y() * 0.62)
            inverse = 1.0 - eased
            state.current = (
                start * (inverse ** 3)
                + control1 * (3.0 * inverse * inverse * eased)
                + control2 * (3.0 * inverse * eased * eased)
                + end * (eased ** 3)
            )
        else:
            state.current = state.animation_start + (state.animation_end - state.animation_start) * eased
        index = self.model().index(state.row, 0)
        if index.isValid():
            self.viewport().update()
        if progress < 1.0:
            return
        self._paper_animation.stop()
        if not state.commit:
            self.cancel_paper_peel()
            return
        record = index.data(AssetItemModel.ItemRole) if index.isValid() else None
        if record is None:
            self.cancel_paper_peel()
            return
        state.waiting_for_result = True
        self.favorite_requested.emit(int(record["id"]), not bool(record["favorite"]))

    def preview_favorite_change(self, item_id: int, value: bool) -> None:
        if not self._asset_model.set_favorite(item_id, value):
            return
        state = self._paper_peel
        row = self._asset_model.row_for_id(item_id)
        if state is None or row < 0 or state.row != row:
            self.viewport().update()
            return
        if self._favorite_page_mode and not value:
            # On the Favorites page the peeled card is the only sheet.  Keep
            # it at its completed (off-card) position until the successful
            # filtered refresh removes the row.  A failed mutation restores
            # it through rollback_favorite_preview().
            state.waiting_for_result = True
            self.viewport().update()
            return
        index = self.model().index(row, 0)
        cell = self.visualRect(index)
        card = self.delegate.card_rect(QRect(0, 0, cell.width(), cell.height()))
        corner = self.delegate.paper_corner(card)
        flat = corner + QPointF(-1.0, 1.0)
        idle = corner + QPointF(-self.delegate.CORNER_SIZE, self.delegate.CORNER_SIZE)
        state.current = flat
        state.press = flat
        state.waiting_for_result = False
        self._animate_paper_peel(idle, 120, False)

    def rollback_favorite_preview(self, item_id: int, value: bool) -> None:
        if self._asset_model.set_favorite(item_id, value):
            self._settle_new_paper_corner(item_id)
            self.viewport().update()

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
                self._start_paper_peel(index, point)
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

    def mouseMoveEvent(self, event) -> None:
        state = self._paper_peel
        if state is not None and self._favorite_press_row == state.row and not state.waiting_for_result:
            index = self.model().index(state.row, 0)
            state.current = self._bounded_paper_point(index, event.position().toPoint())
            self.viewport().update()
            event.accept()
            return
        super().mouseMoveEvent(event)

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
            pressed_row = self._favorite_press_row
            self._favorite_press_row = -1
            state = self._paper_peel
            index = self.model().index(pressed_row, 0)
            if state is not None and index.isValid():
                cell = self._visual_rect_for_index(index)
                card = self.delegate.card_rect(QRect(0, 0, cell.width(), cell.height()))
                corner = self.delegate.paper_corner(card)
                diagonal = max(1.0, math.hypot(card.width(), card.height()))
                travelled = math.hypot(state.current.x() - corner.x(),
                                       state.current.y() - corner.y()) / diagonal
                click = math.hypot(state.current.x() - state.press.x(),
                                   state.current.y() - state.press.y()) < 5.0
                if click or travelled >= 0.42:
                    destination = corner + (QPointF(card.left(), card.bottom()) - corner) * 2.15
                    self._animate_paper_peel(
                        destination,
                        360 if click else 220,
                        True,
                        curved=click,
                    )
                else:
                    idle = self.delegate.paper_corner(card) + QPointF(
                        -self.delegate.CORNER_SIZE,
                        self.delegate.CORNER_SIZE,
                    )
                    self._animate_paper_peel(idle, 160, False)
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
