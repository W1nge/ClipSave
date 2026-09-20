from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer


class SidebarInteractionController(QObject):
    """Own sidebar animation orchestration and collapsed-setting debounce."""

    def __init__(
        self,
        *,
        sidebar,
        grid,
        body_layout,
        settings_get: Callable[[str, object], object],
        save_setting: Callable[[str, object], None],
        detail_animation_active: Callable[[], bool],
        finish_detail_animation: Callable[[], None],
        interactive_resize_active: Callable[[], bool],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.sidebar = sidebar
        self.grid = grid
        self.body_layout = body_layout
        self.settings_get = settings_get
        self.save_setting = save_setting
        self.detail_animation_active = detail_animation_active
        self.finish_detail_animation = finish_detail_animation
        self.interactive_resize_active = interactive_resize_active
        self.animation_active = False
        self.pending_collapsed: bool | None = None
        self.setting_timer = QTimer(parent)
        self.setting_timer.setSingleShot(True)
        self.setting_timer.setInterval(250)
        self.setting_timer.timeout.connect(self.flush_collapsed_setting)

    def connect_signals(self) -> None:
        self.sidebar.collapsed_changed.connect(self.queue_collapsed_setting)
        self.sidebar.width_animation_started.connect(self.begin_animation)
        self.sidebar.width_animation_progress.connect(self.update_animation)
        self.sidebar.width_animation_finished.connect(self.end_animation)

    def begin_animation(self) -> None:
        if self.animation_active:
            return
        if self.detail_animation_active():
            self.finish_detail_animation()
        self.animation_active = True
        self.grid.set_layout_updates_suspended(True)
        self.grid.begin_sidebar_transition(
            self.sidebar.width(),
            self.sidebar.collapse_progress,
        )

    def update_animation(self, progress: float) -> None:
        if not self.animation_active:
            return
        self.body_layout.activate()
        self.grid.set_sidebar_transition_progress(progress)

    def end_animation(self) -> None:
        if not self.animation_active:
            return
        self.animation_active = False
        self.body_layout.activate()
        self.grid.set_layout_updates_suspended(self.interactive_resize_active())
        self.grid.finish_sidebar_transition()

    def queue_collapsed_setting(self, value: bool) -> None:
        value = bool(value)
        if self.pending_collapsed is None and bool(
            self.settings_get("sidebar_collapsed", False)
        ) == value:
            return
        if self.pending_collapsed == value and self.setting_timer.isActive():
            return
        self.pending_collapsed = value
        self.setting_timer.start()

    def flush_collapsed_setting(self, force: bool = False) -> None:
        self.setting_timer.stop()
        if not force and self.animation_active:
            self.setting_timer.start()
            return
        value = self.pending_collapsed
        self.pending_collapsed = None
        if value is None or bool(
            self.settings_get("sidebar_collapsed", False)
        ) == value:
            return
        self.save_setting("sidebar_collapsed", value)
