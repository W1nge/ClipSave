from __future__ import annotations

import ctypes
import hashlib
import hmac
import os
import sqlite3
import sys
import time
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QAbstractNativeEventFilter, QByteArray
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QAbstractSocket as _QAbstractSocket, QLocalServer, QLocalSocket
from .smoke_runtime import SmokeLifecycle
from PySide6.QtWidgets import QApplication, QMessageBox

from .constants import APP_NAME, APP_PATHS, INSTANCE_SERVER
from .main_window import MainWindow
from .runtime import ApplicationRuntime
from .single_instance import (
    SingleInstance as _BaseSingleInstance,
    windows_user_sid as _lookup_windows_user_sid,
)
from .storage import ensure_storage_directories, migrate_legacy_layout
from .startup import set_start_with_windows


SHOW_MESSAGE = b"show\n"
SHOW_ACK = b"ok\n"
GLOBAL_HOTKEY_ID = 0x051A
QAbstractSocket = _QAbstractSocket


def _configure_windows_dpi_awareness() -> bool:
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        ctypes.set_last_error(0)
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return True
        if ctypes.get_last_error() == 5:  # ERROR_ACCESS_DENIED: already configured.
            return True
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        shcore.SetProcessDpiAwareness.restype = ctypes.c_long
        return int(shcore.SetProcessDpiAwareness(2)) >= 0
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _windows_hotkey_api():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.RegisterHotKey.argtypes = [
        wintypes.HWND,
        ctypes.c_int,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.RegisterHotKey.restype = wintypes.BOOL
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.UnregisterHotKey.restype = wintypes.BOOL
    return user32


def _current_user_identity() -> str:
    if os.name == "nt":
        return _windows_user_sid()
    if hasattr(os, "getuid"):
        return str(os.getuid())
    raise RuntimeError("Could not determine a stable current-user identity")


def _windows_user_sid() -> str:
    return _lookup_windows_user_sid()


def _instance_server_name() -> str:
    user_hash = hashlib.sha256(_current_user_identity().encode("utf-8", errors="surrogatepass")).hexdigest()[:24]
    return f"{INSTANCE_SERVER}.{user_hash}"


def _is_show_message(message: bytes) -> bool:
    return hmac.compare_digest(message, SHOW_MESSAGE)


def _claim_or_notify_instance(
    single: "SingleInstance", callback, timeout: float = 2.0
) -> bool | None:
    """Return True for owner, False after notifying owner, or None on timeout."""
    deadline = time.monotonic() + timeout
    while True:
        if single.notify_existing():
            return False
        if single.listen(callback):
            return True
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


def _migration_moved_files(result: dict[str, int]) -> bool:
    return any(type(count) is int and count > 0 for count in result.values())


def _commit_session_data(window, manager) -> None:
    try:
        shutdown = getattr(window, "quit_application_for_session_end", None)
        completed = shutdown is not None and shutdown(timeout=2.0)
    except BaseException:
        completed = False
    if not completed:
        manager.cancel()


def _should_scan_library(migration_result: dict[str, int], database) -> bool:
    if getattr(database, "needs_library_rescan", False) or _migration_moved_files(migration_result):
        return True
    try:
        row = database.connection.execute("SELECT 1 FROM items WHERE missing = 0 LIMIT 1").fetchone()
    except (AttributeError, sqlite3.Error):
        return False
    return row is None


def _smoke_failure(window, uncaught_exceptions: list[str]) -> str | None:
    if uncaught_exceptions:
        return f"uncaught_exception={uncaught_exceptions[0]}"
    startup_error = getattr(window, "startup_scan_error", None)
    if startup_error:
        return f"startup_scan_error={startup_error}"
    return None


def _smoke_backdrop_status(window) -> str:
    backdrop_result = window.window_effects_controller.native_backdrop_result
    backdrop_backend = getattr(
        getattr(backdrop_result, "backend", None), "value", "none"
    )
    backdrop_success = bool(getattr(backdrop_result, "success", False))
    backdrop_error = getattr(backdrop_result, "native_error", None)
    return (
        f"backdrop_backend={backdrop_backend}\n"
        f"backdrop_success={backdrop_success}\n"
        f"backdrop_native_error={backdrop_error}\n"
    )


def _smoke_background_idle(window) -> bool:
    return (
        window.maintenance_controller.startup_request is None
        and window.library_controller.refresh_request is None
        and window.library_controller.search_request is None
        and window.library_controller.page_request is None
    )


def create_app_icon() -> QIcon:
    icon = QIcon()
    for size in (32, 64, 128, 256):
        scale = size / 64
        pixmap = QPixmap(size, size)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#2f7df6"))
        painter.setPen(QColor("#2f7df6"))
        painter.drawRoundedRect(*(round(value * scale) for value in (5, 7, 49, 49, 12, 12)))
        painter.setBrush(QColor("#ffffff"))
        painter.setPen(QColor("#ffffff"))
        painter.drawRoundedRect(*(round(value * scale) for value in (17, 17, 31, 31, 8, 8)))
        painter.setBrush(QColor("#2f7df6"))
        painter.drawRoundedRect(*(round(value * scale) for value in (25, 25, 15, 15, 4, 4)))
        painter.end()
        icon.addPixmap(pixmap)
    return icon


class GlobalHotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def nativeEventFilter(self, event_type: QByteArray, message):
        if os.name == "nt" and event_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            try:
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == 0x0312 and msg.wParam == GLOBAL_HOTKEY_ID:
                    self.callback()
                    return True, 0
            except (TypeError, ValueError):
                pass
        return False, 0


class SingleInstance(_BaseSingleInstance):
    """Compatibility facade preserving app-level QtNetwork patch seams."""

    @classmethod
    def _default_server_name(cls) -> str:
        return _instance_server_name()

    @classmethod
    def _server_api(cls):
        return QLocalServer

    @classmethod
    def _socket_api(cls):
        return QLocalSocket


def main() -> int:
    smoke_profile_path = None
    if "--smoke-profile" in sys.argv:
        index = sys.argv.index("--smoke-profile")
        if index + 1 >= len(sys.argv):
            return 2
        smoke_profile_path = Path(sys.argv[index + 1]).resolve()
        del sys.argv[index : index + 2]
    smoke_ready_path = None
    if "--smoke-ready-file" in sys.argv:
        index = sys.argv.index("--smoke-ready-file")
        if index + 1 >= len(sys.argv):
            return 2
        smoke_ready_path = Path(sys.argv[index + 1])
        del sys.argv[index : index + 2]
    smoke_hold_ms = 0
    if "--smoke-hold-ms" in sys.argv:
        index = sys.argv.index("--smoke-hold-ms")
        if index + 1 >= len(sys.argv):
            return 2
        try:
            smoke_hold_ms = int(sys.argv[index + 1])
        except ValueError:
            return 2
        if not 0 <= smoke_hold_ms <= 30_000:
            return 2
        del sys.argv[index : index + 2]
    if smoke_profile_path is not None and smoke_ready_path is None:
        return 2
    _configure_windows_dpi_awareness()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    icon = create_app_icon()
    app.setWindowIcon(icon)

    smoke_server_name = None
    if smoke_profile_path is not None:
        profile_key = hashlib.sha256(str(smoke_profile_path).encode("utf-8")).hexdigest()[:16]
        smoke_server_name = f"{_instance_server_name()}.Smoke.{profile_key}"
    single = SingleInstance(smoke_server_name)
    window_holder: list[MainWindow] = []
    pending_show = False

    def show_window() -> None:
        nonlocal pending_show
        if window_holder:
            window_holder[0].bring_to_front()
        else:
            pending_show = True

    ownership = _claim_or_notify_instance(single, show_window)
    if ownership is False:
        return 0
    if ownership is None:
        return 1
    app.aboutToQuit.connect(single.close)

    try:
        ensure_storage_directories(APP_PATHS)
        migration_result = migrate_legacy_layout(APP_PATHS)
        ensure_storage_directories(APP_PATHS)
    except (OSError, RuntimeError) as exc:
        QMessageBox.critical(None, "ClipSave 无法启动", str(exc))
        return 1
    try:
        runtime = ApplicationRuntime.create(APP_PATHS)
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        QMessageBox.critical(None, "ClipSave 无法启动", str(exc))
        return 1
    database = runtime.database
    settings = runtime.settings
    startup_error = None
    if smoke_profile_path is None:
        try:
            set_start_with_windows(bool(settings.get("start_with_windows", False)))
        except OSError as exc:
            startup_error = str(exc)
    try:
        window = MainWindow(
            database,
            settings,
            icon,
            scan_on_start=_should_scan_library(migration_result, database),
            reconcile_on_start=True,
            paths=APP_PATHS,
            clipboard_service=runtime.clipboard_service,
            runtime=runtime,
        )
    except Exception as exc:
        runtime.close(timeout=2.0)
        single.close()
        QMessageBox.critical(None, "ClipSave 无法启动", str(exc))
        return 1
    window_holder.append(window)
    app.commitDataRequest.connect(
        lambda manager: _commit_session_data(window, manager)
    )
    if pending_show:
        window.bring_to_front()

    hotkey_filter = GlobalHotkeyFilter(window.focus_search)
    app.installNativeEventFilter(hotkey_filter)
    registered = False
    if os.name == "nt":
        registered = bool(
            _windows_hotkey_api().RegisterHotKey(
                None, GLOBAL_HOTKEY_ID, 0x0002 | 0x0001, ord("V")
            )
        )
    window.global_hotkey_registered = registered
    if startup_error:
        window.show_error_status(f"开机自启动设置无法更新：{startup_error}")
    if os.name == "nt" and not registered:
        window.show_error_status("全局快捷键 Ctrl+Alt+V 注册失败，可能已被其他软件占用")

    smoke_lifecycle = None
    if smoke_ready_path is not None:
        smoke_lifecycle = SmokeLifecycle(
            app,
            window,
            database,
            smoke_ready_path,
            smoke_hold_ms,
            failure=_smoke_failure,
            backdrop_status=_smoke_backdrop_status,
            background_idle=_smoke_background_idle,
        )
        smoke_lifecycle.install()

    window.show()
    if smoke_lifecycle is not None:
        smoke_lifecycle.start()
    exit_code = app.exec()
    if smoke_lifecycle is not None:
        exit_code = smoke_lifecycle.finalize(exit_code)
    elif _smoke_failure(window, []):
        exit_code = exit_code or 1
    if registered:
        _windows_hotkey_api().UnregisterHotKey(None, GLOBAL_HOTKEY_ID)
    if not runtime.close(timeout=2.0):
        exit_code = exit_code or 1
    single.close()
    return exit_code
