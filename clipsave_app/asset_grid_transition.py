from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QModelIndex, QPoint, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QRegion
from PySide6.QtWidgets import QListView, QStyle, QStyleOptionViewItem

from .asset_grid_delegate import format_card_timestamp
from .item_models import AssetItemModel
from .sidebar import Sidebar
from .ui_primitives import dark_theme_active

_TIME_ALIGNMENT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter


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
    full_cache: QPixmap | None = None
    chrome_cache: QPixmap | None = None
    record: object | None = None
    timestamp_text: str | None = None
    paint_option: QStyleOptionViewItem | None = None
    index: QModelIndex | None = None
    text_color: QColor | None = None
    divider_pen: QPen | None = None
    kind: str = ""

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
    MAX_PREVIEW_SAMPLE_RATE = 60.0

    def __init__(
        self,
        view: QListView,
        cards: list[_GridTransitionCard],
        expanded_columns: int,
        collapsed_columns: int,
        initial_progress: float = 0.0,
        cache_full_cards: bool = False,
        clamp_to_viewport: bool = False,
        resize_reflow: bool = False,
    ):
        self.view = view
        self.cards = cards
        self.expanded_columns = expanded_columns
        self.collapsed_columns = collapsed_columns
        self.progress = max(0.0, min(1.0, float(initial_progress)))
        self.cache_full_cards = cache_full_cards
        self.clamp_to_viewport = clamp_to_viewport
        self.resize_reflow = resize_reflow
        screen = view.screen()
        refresh_rate = float(screen.refreshRate()) if screen is not None else 60.0
        if refresh_rate <= 0:
            refresh_rate = 60.0
        # Preview line wrapping changes only at discrete widths. Motion may
        # run at 120 Hz, but re-measuring text 120 times per second merely
        # delays the first frame without adding intermediate visual states.
        refresh_rate = min(self.MAX_PREVIEW_SAMPLE_RATE, refresh_rate)
        self.preview_frame_count = max(
            2,
            round(Sidebar.ANIMATION_DURATION_MS * refresh_rate / 1000.0) + 1,
        )
        self._cards_by_row = {card.row: card for card in cards}
        self._paint_cards = [
            *(card for card in cards if not card.elevated),
            *(card for card in cards if card.elevated),
        ]
        self._chrome_caches: dict[tuple[object, ...], QPixmap] = {}
        self._prepare_preview_caches()

    @property
    def paint_order_rows(self) -> list[int]:
        return [card.row for card in self._paint_cards]

    @property
    def elevated_rows(self) -> list[int]:
        return [card.row for card in self._paint_cards if card.elevated]

    def _prepare_preview_caches(self) -> None:
        visible = QRectF(self.view.viewport().rect())
        for card in self.cards:
            # Keep the card in the transition for hit testing and scrolling,
            # but avoid eager text layout for rows that never enter the view.
            if not card.expanded_rect.united(card.collapsed_rect).intersects(visible):
                continue
            index = self.view.model().index(card.row, 0)
            if not index.isValid():
                continue
            record = index.data(AssetItemModel.ItemRole)
            if record is None:
                continue
            card.record = record
            card.index = index
            card.timestamp_text = format_card_timestamp(record["last_used_at"])
            card.kind = str(record["kind"])
            favorite = bool(record["favorite"])
            dark = dark_theme_active()
            card.text_color = QColor(
                "#34404e" if favorite else "#d9e1eb" if dark else "#45576a"
            )
            card.divider_pen = QPen(
                QColor(53, 62, 73, 48) if favorite
                else QColor(142, 170, 200, 36) if dark
                else QColor(57, 76, 96, 34),
                1,
            )
            option = QStyleOptionViewItem()
            self.view.initViewItemOption(option)
            option.widget = self.view
            option.state |= QStyle.StateFlag.State_Active | QStyle.StateFlag.State_Enabled
            card.paint_option = option
            if self.cache_full_cards and card.expanded_rect.size() == card.collapsed_rect.size():
                self._cache_full_card(card, index)
                continue
            initial_size = _grid_transition_rect(card, self.progress).size()
            card.chrome_cache = self._chrome_cache(card.row, record, initial_size)
            state_by_signature: dict[tuple[object, ...], int] = {}
            if self.resize_reflow and record["kind"] != "image":
                # A resize breakpoint can involve dozens of cards. Preparing
                # every wrapping width before its first frame stalls the UI.
                # Start with one current-width preview; preview_cache updates
                # it lazily when the moving card crosses a wrap threshold.
                sample_progresses = [self.progress]
            elif record["kind"] == "image":
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
                elif record["kind"] != "image":
                    state = card.preview_states[state_index]
                    if state.cache is not None and not self.view.delegate.transition_preview_covers(
                        state.cache, cell_size,
                    ):
                        state.cache = self.view.delegate.render_transition_preview(
                            index, cell_size, signature,
                        )
                card.preview_samples.append(state_index)

            if record["kind"] == "image" or self.resize_reflow:
                card.preview_samples *= self.preview_frame_count

    def _chrome_cache(
        self, row: int, record, size,
    ) -> QPixmap:
        width = max(1, round(size.width()))
        height = max(1, round(size.height()))
        dpr = max(1.0, float(self.view.devicePixelRatioF()))
        key = (
            width, height, dpr, bool(record["favorite"]),
            bool(getattr(self.view, "favorite_page_mode", False)), dark_theme_active(),
        )
        cached = self._chrome_caches.get(key)
        if cached is not None:
            return cached
        cached = QPixmap(max(1, round(width * dpr)), max(1, round(height * dpr)))
        cached.setDevicePixelRatio(dpr)
        cached.fill(QColor(0, 0, 0, 0))
        option = QStyleOptionViewItem()
        self.view.initViewItemOption(option)
        option.rect = QRect(0, 0, width, height)
        option.widget = self.view
        option.state |= QStyle.StateFlag.State_Active | QStyle.StateFlag.State_Enabled
        painter = QPainter(cached)
        self.view.delegate.paint_transition_card(
            painter, option, self.view.model().index(row, 0), paint_content=False,
        )
        painter.end()
        self._chrome_caches[key] = cached
        return cached

    def _cache_full_card(self, card: _GridTransitionCard, index: QModelIndex) -> None:
        width = max(1, round(card.collapsed_rect.width()))
        height = max(1, round(card.collapsed_rect.height()))
        option = QStyleOptionViewItem()
        self.view.initViewItemOption(option)
        option.rect = QRect(0, 0, width, height)
        option.widget = self.view
        option.state |= QStyle.StateFlag.State_Active | QStyle.StateFlag.State_Enabled
        if self.view.selectionModel().isSelected(index):
            option.state |= QStyle.StateFlag.State_Selected
        else:
            option.state &= ~QStyle.StateFlag.State_Selected
        cached = self.view.delegate.cached_card_pixmap(option, index)
        if cached is not None:
            card.full_cache = cached
            return
        dpr = max(1.0, float(self.view.devicePixelRatioF()))
        cache = QPixmap(max(1, round(width * dpr)), max(1, round(height * dpr)))
        cache.setDevicePixelRatio(dpr)
        cache.fill(QColor(0, 0, 0, 0))
        painter = QPainter(cache)
        self.view.delegate.paint_transition_card(painter, option, index)
        painter.end()
        card.full_cache = cache

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
        if self.resize_reflow and card.kind != "image":
            # The target can change again before this animation finishes.
            # Text and paper must use the same interpolated size, including
            # on the last frame before the live delegate takes over.
            target = self._card_rect(card, self.progress if progress is None else progress)
            size = QSize(max(1, round(target.width())), max(1, round(target.height())))
            index = card.index or self.view.model().index(card.row, 0)
            signature = self.view.delegate.transition_layout_signature(index, size)
            state = card.preview_states[state_index]
            if (state.signature != signature or state.cache is None
                    or not self.view.delegate.transition_preview_covers(state.cache, size)):
                state.cache = self.view.delegate.render_transition_preview(index, size, signature)
                state.signature = signature
        return card.preview_states[state_index].cache

    def set_progress(self, progress: float) -> QRegion:
        progress = max(0.0, min(1.0, float(progress)))
        if abs(progress - self.progress) < 1e-6:
            return QRegion()
        previous = self.progress
        self.progress = progress
        dirty = QRegion()
        visible = QRectF(self.view.viewport().rect())
        for card in self.cards:
            if not card.expanded_rect.united(card.collapsed_rect).intersects(visible):
                continue
            dirty |= QRegion(
                self._card_rect(card, previous)
                .toAlignedRect()
                .adjusted(-2, -2, 2, 2)
            )
            dirty |= QRegion(
                self._card_rect(card, progress)
                .toAlignedRect()
                .adjusted(-2, -2, 2, 2)
            )
        return dirty

    def card_rect(self, row: int) -> QRectF | None:
        card = self._cards_by_row.get(row)
        return None if card is None else self._card_rect(card, self.progress)

    def _card_rect(self, card: _GridTransitionCard, progress: float) -> QRectF:
        rect = _grid_transition_rect(card, progress)
        if self.clamp_to_viewport:
            rect.moveLeft(min(
                max(0.0, rect.left()),
                max(0.0, self.view.viewport().width() - rect.width()),
            ))
        return rect

    def index_at(self, point: QPoint) -> QModelIndex:
        point_f = QPointF(point)
        for card in reversed(self._paint_cards):
            if self._card_rect(card, self.progress).contains(point_f):
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
        painter.setClipRect(rect, Qt.ClipOperation.IntersectClip)
        for card in self._paint_cards:
            target = self._card_rect(card, self.progress)
            if not target.intersects(QRectF(rect)):
                continue
            if card.full_cache is not None:
                dpr = max(1.0, float(self.view.devicePixelRatioF()))
                painter.drawPixmap(
                    QPointF(round(target.x() * dpr) / dpr,
                            round(target.y() * dpr) / dpr),
                    card.full_cache,
                )
                continue
            if card.chrome_cache is not None:
                dpr = max(1.0, float(self.view.devicePixelRatioF()))
                if card.record is None:
                    continue
                cache = self._chrome_cache(card.row, card.record, target.size())
                card.chrome_cache = cache
                snapped = QPointF(
                    round(target.x() * dpr) / dpr,
                    round(target.y() * dpr) / dpr,
                )
                painter.drawPixmap(snapped, cache)
                self._draw_card(painter, card, target, content_only=True)
                continue
            self._draw_card(
                painter,
                card,
                target,
            )

    def _draw_card(
        self,
        painter: QPainter,
        card: _GridTransitionCard,
        target: QRectF,
        content_only: bool = False,
    ) -> None:
        index = card.index or self.view.model().index(card.row, 0)
        if not index.isValid():
            return
        option = card.paint_option or QStyleOptionViewItem()
        if card.paint_option is None:
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
        if content_only:
            self._paint_cached_content(painter, option, card, index)
        else:
            self.view.delegate.paint_transition_card(
                painter, option, index, self.preview_cache(card),
            )
        painter.restore()

    def _paint_cached_content(
        self, painter: QPainter, option: QStyleOptionViewItem,
        card: _GridTransitionCard, index: QModelIndex,
    ) -> None:
        cache = self.preview_cache(card)
        if cache is None or card.text_color is None or card.divider_pen is None:
            self.view.delegate.paint_transition_content(
                painter, option, index, cache,
                record=card.record, timestamp_text=card.timestamp_text,
            )
            return
        cell = option.rect
        paper = cell.adjusted(6, 6, -6, -6)
        left_inset = max(12, round(paper.width() * 0.08))
        time_rect = QRect(
            paper.left() + left_inset - 1,
            paper.top() + 9,
            max(1, paper.width() - left_inset - 36),
            24,
        )
        painter.setFont(option.font)
        painter.setPen(card.text_color)
        painter.drawText(time_rect, _TIME_ALIGNMENT, card.timestamp_text or "")
        painter.setPen(card.divider_pen)
        divider_y = paper.top() + 41
        painter.drawLine(paper.left() + 12, divider_y, paper.right() - 12, divider_y)
        preview = paper.adjusted(12, 54, -12, -12)
        painter.setClipRect(preview, Qt.ClipOperation.IntersectClip)
        if card.kind == "image":
            painter.drawPixmap(
                self.view.delegate.transition_image_target(preview, cache),
                cache, QRectF(cache.rect()),
            )
        else:
            painter.drawPixmap(preview.topLeft(), cache)


GridTransitionCard = _GridTransitionCard
AssetGridTransitionOverlay = _AssetGridTransitionOverlay
grid_transition_card_elevated = _grid_transition_card_elevated
