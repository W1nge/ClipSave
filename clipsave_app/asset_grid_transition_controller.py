from __future__ import annotations

from PySide6.QtCore import QRectF, QSize

from .asset_grid_delegate import AssetGridDelegate
from .asset_grid_transition import (
    AssetGridTransitionOverlay,
    GridTransitionCard,
    grid_transition_card_elevated,
)
from .sidebar import Sidebar


class AssetGridTransitionController:
    """Own sidebar/viewport transition geometry and overlay lifecycle."""

    def __init__(self, view) -> None:
        self.view = view
        self.active = False
        self.overlay: AssetGridTransitionOverlay | None = None

    @staticmethod
    def layout_for_viewport_width(width: int) -> tuple[int, QSize]:
        available = max(210, int(width))
        columns = max(1, available // 245)
        gap = 12
        layout_width = max(210, available - 1)
        card_width = max(210, (layout_width - columns * gap) // columns)
        # At the narrowest widths a 210px card plus the gap would exceed the
        # viewport (whose scrollbars are hidden); clamp the card so the cell
        # always fits instead of clipping the card's right edge.
        card_width = min(card_width, max(120, layout_width - gap))
        card_height = max(170, round(card_width / AssetGridDelegate.CARD_ASPECT))
        return columns, QSize(
            card_width + gap,
            card_height + gap,
        )

    @staticmethod
    def transition_cell_rect(
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

    def transition_rows(
        self,
        columns: int,
        grid_size: QSize,
        scroll_offset: int,
    ) -> range:
        count = self.view.model().rowCount()
        if count <= 0:
            return range(0)
        first_layout_row = max(0, scroll_offset // grid_size.height() - 1)
        last_layout_row = (
            scroll_offset + self.view.viewport().height()
        ) // grid_size.height() + 1
        return range(
            min(count, first_layout_row * columns),
            min(count, (last_layout_row + 1) * columns),
        )

    def begin_sidebar(
        self,
        current_sidebar_width: int,
        progress: float,
        expanded_sidebar_width: int = Sidebar.EXPANDED_WIDTH,
        collapsed_sidebar_width: int = Sidebar.COLLAPSED_WIDTH,
    ) -> bool:
        self.clear(repaint=False)
        if not self._can_begin():
            return False
        current_sidebar_width = max(
            collapsed_sidebar_width,
            min(expanded_sidebar_width, int(current_sidebar_width)),
        )
        current_width = self.view.viewport().width()
        expanded_width = max(
            1,
            current_width - (expanded_sidebar_width - current_sidebar_width),
        )
        collapsed_width = max(
            1,
            current_width + (current_sidebar_width - collapsed_sidebar_width),
        )
        return self.begin_viewport(expanded_width, collapsed_width, progress)

    def begin_viewport(
        self,
        start_viewport_width: int,
        end_viewport_width: int,
        progress: float = 0.0,
    ) -> bool:
        self.clear(repaint=False)
        if not self._can_begin():
            return False
        start_columns, start_size = self.layout_for_viewport_width(
            max(1, int(start_viewport_width))
        )
        end_columns, end_size = self.layout_for_viewport_width(
            max(1, int(end_viewport_width))
        )
        scroll_offset = self.view.verticalScrollBar().value()
        rows = sorted(
            set(self.transition_rows(start_columns, start_size, scroll_offset))
            | set(self.transition_rows(end_columns, end_size, scroll_offset))
        )
        if not rows:
            return False
        cards = [
            GridTransitionCard(
                row=row,
                expanded_rect=self.transition_cell_rect(
                    row,
                    start_columns,
                    start_size,
                    scroll_offset,
                ),
                collapsed_rect=self.transition_cell_rect(
                    row,
                    end_columns,
                    end_size,
                    scroll_offset,
                ),
                elevated=grid_transition_card_elevated(
                    row,
                    start_columns,
                    end_columns,
                ),
            )
            for row in rows
        ]
        self.overlay = AssetGridTransitionOverlay(
            self.view,
            cards,
            start_columns,
            end_columns,
            progress,
        )
        self.active = True
        return True

    def set_progress(self, progress: float) -> None:
        overlay = self.overlay
        if not self.active or overlay is None:
            return
        dirty = overlay.set_progress(progress)
        if not dirty.isEmpty():
            self.view.viewport().update(dirty)

    def begin_reflow(self, cards: list[GridTransitionCard]) -> bool:
        """Animate an already-updated model from captured cells to its new layout."""
        self.clear(repaint=False)
        if not cards or not self._can_begin():
            return False
        columns = max(1, int(self.view.columns))
        self.overlay = AssetGridTransitionOverlay(
            self.view,
            cards,
            columns,
            columns,
            0.0,
        )
        self.active = True
        return True

    def clear(self, repaint: bool) -> None:
        if self.overlay is None and not self.active:
            return
        self.active = False
        self.overlay = None
        if repaint and self.view.viewport().isVisible():
            self.view.viewport().repaint()

    def _can_begin(self) -> bool:
        return bool(
            self.view.isVisible()
            and self.view.model().rowCount() > 0
            and self.view.viewport().width() > 0
            and self.view.viewport().height() > 0
        )
