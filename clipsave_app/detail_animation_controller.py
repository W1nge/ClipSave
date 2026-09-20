from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QElapsedTimer, QEvent, QObject, QTimer, Qt
from PySide6.QtWidgets import QApplication

from .sidebar import Sidebar


class DetailAnimationController(QObject):
    """Own the detail-pane width animation and splitter width memory."""

    def __init__(
        self,
        window,
        *,
        detail,
        grid,
        content_splitter,
        body_layout,
        detail_button,
        sidebar,
        sidebar_animation_active: Callable[[], bool],
        interactive_resize_active: Callable[[], bool],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.window = window
        self.detail = detail
        self.grid = grid
        self.content_splitter = content_splitter
        self.body_layout = body_layout
        self.detail_button = detail_button
        self.sidebar = sidebar
        self.sidebar_animation_active = sidebar_animation_active
        self.interactive_resize_active = interactive_resize_active

        self.active = False
        self.target_visible = False
        self.progress = 0.0
        self.start_progress = 0.0
        self.end_progress = 0.0
        self.target_width = 340
        self.saved_width = 340
        self.timer = QTimer(parent)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self.advance)
        self.elapsed = QElapsedTimer()

    def desired_width(self) -> int:
        total = max(
            1,
            self.content_splitter.width() - self.content_splitter.handleWidth(),
        )
        desired = min(520, max(280, self.saved_width))
        if total > 0:
            desired = min(desired, max(280, total - 240))
        return max(1, desired)

    def start(self, visible: bool) -> None:
        visible = bool(visible)
        end_progress = 1.0 if visible else 0.0
        if not self.active and abs(self.progress - end_progress) < 1e-6:
            self.target_visible = visible
            self.detail.setVisible(visible)
            self.detail_button.setToolTip("收起详情" if visible else "显示详情")
            return
        if self.sidebar_animation_active():
            self.sidebar.set_collapsed(self.sidebar.collapsed, animate=False)

        self.target_visible = visible
        self.timer.stop()
        target_width = self.desired_width()
        self.target_width = target_width
        self.detail.setMinimumWidth(0)
        self.detail.setMaximumWidth(target_width)
        self.content_splitter.setCollapsible(1, True)
        self.detail.setVisible(True)
        self.body_layout.activate()
        pane_total = max(
            1,
            self.content_splitter.width() - self.content_splitter.handleWidth(),
        )
        if pane_total > 0:
            target_width = min(target_width, max(1, pane_total - 240))
            self.target_width = target_width
            self.detail.setMaximumWidth(target_width)

        starting_transaction = not self.active
        if not self.active:
            self.active = True
            self.grid.set_layout_updates_suspended(True)

        self.apply_panel_width(0)
        hidden_viewport_width = self.grid.viewport().width()
        self.apply_panel_width(target_width)
        shown_viewport_width = self.grid.viewport().width()
        if starting_transaction:
            self.detail.begin_width_transition(
                target_width,
                self.detail.viewport().width(),
            )
            self.body_layout.activate()
            QApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
            self.detail.synchronize_width_transition_content()

        self.apply_panel_width(round(target_width * self.progress))
        self.grid.begin_viewport_width_transition(
            hidden_viewport_width,
            shown_viewport_width,
            self.progress,
        )
        self.start_progress = self.progress
        self.end_progress = end_progress
        screen = self.window.screen()
        refresh_rate = float(screen.refreshRate()) if screen is not None else 60.0
        if refresh_rate < 30.0:
            refresh_rate = 60.0
        refresh_rate = min(Sidebar.MAX_ANIMATION_REFRESH_RATE, refresh_rate)
        self.timer.setInterval(max(1, round(1000.0 / refresh_rate)))
        self.elapsed.start()
        self.timer.start()
        self.detail_button.setToolTip("收起详情" if visible else "显示详情")

    def advance(self) -> None:
        if not self.active:
            self.timer.stop()
            return
        elapsed_ms = self.elapsed.nsecsElapsed() / 1_000_000.0
        fraction = min(1.0, elapsed_ms / Sidebar.ANIMATION_DURATION_MS)
        progress = self.start_progress + (
            self.end_progress - self.start_progress
        ) * fraction
        self.set_progress(progress)
        if fraction >= 1.0:
            self.finish()

    def set_progress(self, progress: float) -> None:
        self.progress = max(0.0, min(1.0, float(progress)))
        width = round(self.target_width * self.progress)
        self.apply_panel_width(width)
        self.grid.set_sidebar_transition_progress(self.progress)

    def apply_panel_width(self, width: int) -> None:
        total = max(
            1,
            self.content_splitter.width() - self.content_splitter.handleWidth(),
        )
        self.content_splitter.setSizes(
            [max(0, total - width), max(0, width)]
        )
        self.body_layout.activate()

    def finish(self) -> None:
        self.timer.stop()
        endpoint = 1.0 if self.target_visible else 0.0
        self.set_progress(endpoint)
        self.active = False
        self.grid.set_layout_updates_suspended(self.interactive_resize_active())
        self.grid.finish_sidebar_transition()
        self.detail.finish_width_transition()
        self.detail.setMinimumWidth(280)
        self.detail.setMaximumWidth(520)
        if self.target_visible:
            self.restore_splitter_size()
        else:
            self.detail.setVisible(False)
        self.content_splitter.setCollapsible(1, False)
        self.body_layout.activate()

    def splitter_moved(self, _position: int, _index: int) -> None:
        if self.detail.isVisible() and not self.active:
            self.saved_width = max(self.detail.minimumWidth(), self.detail.width())

    def restore_splitter_size(self) -> None:
        if self.active:
            return
        total = max(
            1,
            self.content_splitter.width() - self.content_splitter.handleWidth(),
        )
        if total <= 0:
            QTimer.singleShot(0, self.restore_splitter_size)
            return
        desired = min(
            self.detail.maximumWidth(),
            max(self.detail.minimumWidth(), self.saved_width),
        )
        desired = min(desired, max(self.detail.minimumWidth(), total - 240))
        self.content_splitter.setSizes([max(1, total - desired), desired])
