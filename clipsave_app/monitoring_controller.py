from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MonitoringToggleStatus(Enum):
    SUCCESS = "success"
    SETTING_SAVE_FAILED = "setting_save_failed"
    RUNTIME_FAILED = "runtime_failed"
    STATE_MISMATCH = "state_mismatch"
    PERSISTENCE_WARNING = "persistence_warning"


@dataclass(frozen=True, slots=True)
class MonitoringToggleResult:
    status: MonitoringToggleStatus
    active: bool
    message: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status is MonitoringToggleStatus.SUCCESS


class MonitoringController:
    """Own the settings/runtime transaction for clipboard monitoring state."""

    def __init__(self, settings, clipboard_service) -> None:
        self.settings = settings
        self.clipboard_service = clipboard_service

    def toggle(self) -> MonitoringToggleResult:
        previous = self.clipboard_service.timer.isActive()
        desired = not previous
        save_warning: Exception | None = None

        try:
            self.settings.set("monitoring", desired)
        except Exception as exc:
            if bool(self.settings.get("monitoring", previous)) != desired:
                return MonitoringToggleResult(
                    MonitoringToggleStatus.SETTING_SAVE_FAILED,
                    previous,
                    f"设置无法保存：{exc}",
                )
            save_warning = exc

        try:
            self._set_runtime(desired)
        except Exception as exc:
            rollback_errors: list[str] = []
            try:
                self.settings.set("monitoring", previous)
            except Exception as rollback_exc:
                rollback_errors.append(str(rollback_exc))
            try:
                self._set_runtime(previous)
            except Exception as rollback_exc:
                rollback_errors.append(str(rollback_exc))
            suffix = (
                f"；回滚失败：{'；'.join(rollback_errors)}"
                if rollback_errors
                else ""
            )
            return MonitoringToggleResult(
                MonitoringToggleStatus.RUNTIME_FAILED,
                previous,
                f"本地自动捕获无法切换：{exc}{suffix}",
            )

        if self.clipboard_service.timer.isActive() != desired:
            try:
                self.settings.set("monitoring", previous)
            except Exception as exc:
                return MonitoringToggleResult(
                    MonitoringToggleStatus.STATE_MISMATCH,
                    previous,
                    f"本地自动捕获状态异常，且设置无法回滚：{exc}",
                )
            return MonitoringToggleResult(
                MonitoringToggleStatus.STATE_MISMATCH,
                previous,
                "本地自动捕获未能切换到请求的状态",
            )

        if save_warning is not None:
            return MonitoringToggleResult(
                MonitoringToggleStatus.PERSISTENCE_WARNING,
                desired,
                f"设置已写入，但持久化确认失败：{save_warning}",
            )
        return MonitoringToggleResult(MonitoringToggleStatus.SUCCESS, desired)

    def _set_runtime(self, active: bool) -> None:
        if active:
            self.clipboard_service.start()
        else:
            self.clipboard_service.stop()
