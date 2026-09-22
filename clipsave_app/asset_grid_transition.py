from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QModelIndex, QPoint, QPointF, QRect, QRectF, QSize
from PySide6.QtGui import QColor, QPainter, QPixmap, QRegion
from PySide6.QtWidgets import QListView, QStyle, QStyleOptionViewItem

from .item_models import AssetItemModel
from .sidebar import Sidebar
from .ui_primitives import dark_theme_active


@dataclass(slots=True)
class _GridTransitionPreviewState:
    signature: tuple[object, ...]
    cache: QPixmap | None

@dataclass(slots=True)
class _GridTransitionCard:
    row: int
    expanded_rect: QRectF
    collapsed_rect: QRectF
    elevated: bool = False
    preview_states: list[_GridTransitionPreviewState] = field(default_factory=list)
    preview_samples: list[int] = field(default_factory=list)

def _interpolate_rect(start: QRectF, end: QRectF, progress: float) -> QRectF:
    return QRectF(
        start.x() + (end.x() - start.x()) * progress,
        start.y() + (end.y() - start.y()) * progress,
        start.width() + (end.width() - start.width()) * progress,
        start.height() + (end.height() - start.height()) * progress,
    )


def _grid_transition_rect(card: _GridTransitionCard, progress: float) -> QRectF:
    return _interpolate_rect(card.expanded_rect, card.collapsed_rect, progress)

def _grid_transition_card_elevated(
    row: int,
    expanded_columns: int,
    collapsed_columns: int,
) -> bool:
    if expanded_columns == collapsed_columns:
        return False
    if collapsed_columns > expanded_columns:
        return row % collapsed_columns >= expanded_columns
    return row % expanded_columns >= collapsed_columns

class _AssetGridTransitionOverlay:
    def __init__(
        self,
        view: QListView,
        cards: list[_GridTransitionCard],
        expanded_columns: int,
        collapsed_columns: int,
        initial_progress: float = 0.0,
    ):
        self.view = view
        self.cards = cards
        self.expanded_columns = expanded_columns
        self.collapsed_columns = collapsed_columns
        self.progress = max(0.0, min(1.0, float(initial_progress)))
        screen = view.screen()
        refresh_rate = float(screen.refreshRate()) if screen is not None else 60.0
        if refresh_rate <= 0:
            refresh_rate = 60.0
        refresh_rate = min(Sidebar.MAX_ANIMATION_REFRESH_RATE, refresh_rate)
        self.preview_frame_count = max(
            2,
            round(Sidebar.ANIMATION_DURATION_MS * refresh_rate / 1000.0) + 1,
        )
        self._cards_by_row = {card.row: card for card in cards}
        self._paint_cards = [
            *(card for card in cards if not card.elevated),
            *(card for card in cards if card.elevated),
        ]
        self._prepare_preview_caches()

    @property
    def paint_order_rows(self) -> list[int]:
        return [card.row for card in self._paint_cards]

    @property
    def elevated_rows(self) -> list[int]:
        return [card.row for card in self._paint_cards if card.elevated]

    def _prepare_preview_caches(self) -> None:
        for card in self.cards:
            index = self.view.model().index(card.row, 0)
            if not index.isValid():
                continue
            record = index.data(AssetItemModel.ItemRole)
            if record is None:
                continue
            state_by_signature: dict[tuple[object, ...], int] = {}
            if record["kind"] == "image":
                sample_progresses = [
                    max(
                        (0.0, 1.0),
                        key=lambda value: (
                            _grid_transition_rect(card, value).width()
                            * _grid_transition_rect(card, value).height()
                        ),
                    )
                ]
            else:
                sample_progresses = [
                    frame / (self.preview_frame_count - 1)
                    for frame in range(self.preview_frame_count)
                ]
            for sample_progress in sample_progresses:
                target = _grid_transition_rect(card, sample_progress)
                cell_size = QSize(
                    max(1, round(target.width())),
                    max(1, round(target.height())),
                )
                signature = self.view.delegate.transition_layout_signature(
                    index,
                    cell_size,
                )
                state_index = state_by_signature.get(signature)
                if state_index is None:
                    state_index = len(card.preview_states)
                    state_by_signature[signature] = state_index
                    card.preview_states.append(
                        _GridTransitionPreviewState(
                            signature,
                            self.view.delegate.render_transition_preview(
                                index,
                                cell_size,
                                signature,
                            ),
                        )
                    )
                card.preview_samples.append(state_index)

            if record["kind"] == "image":
                card.preview_samples *= self.preview_frame_count

    def preview_state_index(
        self,
        card: _GridTransitionCard,
        progress: float | None = None,
    ) -> int:
        if not card.preview_samples:
            return -1
        value = self.progress if progress is None else progress
        sample = round(
            max(0.0, min(1.0, float(value)))
            * (len(card.preview_samples) - 1)
        )
        return card.preview_samples[sample]

    def preview_cache(
        self,
        card: _GridTransitionCard,
        progress: float | None = None,
    ) -> QPixmap | None:
        state_index = self.preview_state_index(card, progress)
        if state_index < 0:
            return None
        return card.preview_states[state_index].cache

    def set_progress(self, progress: float) -> QRegion:
        progress = max(0.0, min(1.0, float(progress)))
        if abs(progress - self.progress) < 1e-6:
            return QRegion()
        previous = self.progress
        self.progress = progress
        dirty = QRegion()
        for card in self.cards:
            dirty |= QRegion(
                _grid_transition_rect(card, previous)
                .toAlignedRect()
                .adjusted(-2, -2, 2, 2)
            )
            dirty |= QRegion(
                _grid_transition_rect(card, progress)
                .toAlignedRect()
                .adjusted(-2, -2, 2, 2)
            )
        return dirty

    def card_rect(self, row: int) -> QRectF | None:
        card = self._cards_by_row.get(row)
        return None if card is None else _grid_transition_rect(card, self.progress)

    def index_at(self, point: QPoint) -> QModelIndex:
        point_f = QPointF(point)
        for card in reversed(self._paint_cards):
            if _grid_transition_rect(card, self.progress).contains(point_f):
                return self.view.model().index(card.row, 0)
        return QModelIndex()

    def paint(self, painter: QPainter, rect: QRect) -> None:
        # The home surface is deliberately opaque.  Clear every transition
        # frame before compositing moving cards so the transparent item view
        # cannot retain cards from an earlier column layout in Qt's backing
        # store (the stale cells appeared as an empty first row).
        painter.fillRect(
            rect,
            QColor("#202020" if dark_theme_active() else "#f6f6f6"),
        )
        painter.setClipRect(rect)
        for card in self._paint_cards:
            self._draw_card(
                painter,
                card,
                _grid_transition_rect(card, self.progress),
            )

    def _draw_card(
        self,
        painter: QPainter,
        card: _GridTransitionCard,
        target: QRectF,
    ) -> None:
        index = self.view.model().index(card.row, 0)
        if not index.isValid():
            return
        option = QStyleOptionViewItem()
        self.view.initViewItemOption(option)
        option.rect = QRect(
            0,
            0,
            max(1, round(target.width())),
            max(1, round(target.height())),
        )
        option.widget = self.view
        option.state |= QStyle.StateFlag.State_Active | QStyle.StateFlag.State_Enabled
        if self.view.selectionModel().isSelected(index):
            option.state |= QStyle.StateFlag.State_Selected
        else:
            option.state &= ~QStyle.StateFlag.State_Selected
        painter.save()
        # Header text is painted live while preview content comes from cached
        # pixmaps.  Translating the live text through fractional device pixels
        # changes its antialiasing on every animation frame and makes the time
        # appear to flash.  Keep the interpolated size, but snap the card's
        # paint origin to whole pixels so glyph rasterization stays stable.
        dpr = max(1.0, float(self.view.devicePixelRatioF()))
        painter.translate(
            round(target.x() * dpr) / dpr,
            round(target.y() * dpr) / dpr,
        )
        self.view.delegate.paint_transition_card(
            painter,
            option,
            index,
            self.preview_cache(card),
        )
        painter.restore()


GridTransitionCard = _GridTransitionCard
AssetGridTransitionOverlay = _AssetGridTransitionOverlay
grid_transition_card_elevated = _grid_transition_card_elevated
