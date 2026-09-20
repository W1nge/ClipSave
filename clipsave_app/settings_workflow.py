from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .settings import Settings


@dataclass(frozen=True, slots=True)
class SettingsSnapshot:
    follow_system_theme: bool
    theme_mode: str
    start_with_windows: bool
    auto_ocr: bool
    auto_description: bool


@dataclass(frozen=True, slots=True)
class SettingsEffects:
    startup_error: str = ""
    theme_changed: bool = False
    cancel_auto_ocr: bool = False
    cancel_auto_description: bool = False


class SettingsWorkflow:
    """Own post-dialog settings reconciliation and rollback rules."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def snapshot(self) -> SettingsSnapshot:
        return SettingsSnapshot(
            follow_system_theme=bool(
                self.settings.get("follow_system_theme", True)
            ),
            theme_mode=str(self.settings.get("theme_mode", "light")),
            start_with_windows=bool(
                self.settings.get("start_with_windows", False)
            ),
            auto_ocr=bool(self.settings.get("auto_ocr", False)),
            auto_description=bool(
                self.settings.get("auto_description", False)
            ),
        )

    def reconcile(
        self,
        previous: SettingsSnapshot,
        *,
        accepted: bool,
        set_startup: Callable[[bool], None],
    ) -> SettingsEffects:
        if not accepted:
            return SettingsEffects()

        current_startup = bool(
            self.settings.get("start_with_windows", False)
        )
        startup_error = ""
        if current_startup != previous.start_with_windows:
            try:
                set_startup(current_startup)
            except OSError as exc:
                startup_error = str(exc)
                try:
                    self.settings.set(
                        "start_with_windows",
                        previous.start_with_windows,
                    )
                except OSError:
                    pass

        current_follow_system = bool(
            self.settings.get("follow_system_theme", True)
        )
        current_theme_mode = str(
            self.settings.get("theme_mode", "light")
        )
        current_auto_ocr = bool(self.settings.get("auto_ocr", False))
        current_auto_description = bool(
            self.settings.get("auto_description", False)
        )
        return SettingsEffects(
            startup_error=startup_error,
            theme_changed=(
                current_follow_system != previous.follow_system_theme
                or current_theme_mode != previous.theme_mode
            ),
            cancel_auto_ocr=(
                previous.auto_ocr and not current_auto_ocr
            ),
            cancel_auto_description=(
                previous.auto_description and not current_auto_description
            ),
        )
