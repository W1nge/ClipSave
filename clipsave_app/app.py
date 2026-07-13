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

from PySide6.QtCore import QAbstractNativeEventFilter, QByteArray, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QAbstractSocket, QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox

from .constants import APP_NAME, INSTANCE_SERVER
from .database import LibraryDatabase
from .main_window import MainWindow
from .settings import Settings
from .storage import ensure_storage_directories, migrate_legacy_layout
from .startup import set_start_with_windows


SHOW_MESSAGE = b"show\n"
SHOW_ACK = b"ok\n"
GLOBAL_HOTKEY_ID = 0x051A


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
    token_query = 0x0008
    token_user_class = 1

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]

    class TokenUser(ctypes.Structure):
        _fields_ = [("user", SidAndAttributes)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(
            token, token_user_class, None, 0, ctypes.byref(required)
        )
        if not required.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            token_user_class,
            buffer,
            required.value,
            ctypes.byref(required),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        token_user = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(token_user.user.sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not sid_text.value:
                raise RuntimeError("Windows returned an empty current-user SID")
            return sid_text.value
        finally:
            kernel32.LocalFree(sid_text)
    finally:
        kernel32.CloseHandle(token)


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


class SingleInstance:
    MAX_CLIENTS = 16
    CLIENT_TIMEOUT_MS = 250

    def __init__(self, server_name: str | None = None):
        self.server_name = server_name or _instance_server_name()
        self.server = None
        self._mutex_handle = None
        self._connections: dict[object, bytearray] = {}

    @property
    def mutex_name(self) -> str:
        digest = hashlib.sha256(self.server_name.encode("utf-8")).hexdigest()[:32]
        return f"Global\\ClipSave.Instance.{digest}"

    def _acquire_mutex(self) -> bool:
        if os.name != "nt":
            return True
        if self._mutex_handle is not None:
            return True
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateMutexW(None, False, self.mutex_name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        self._mutex_handle = handle
        return True

    def _release_mutex(self) -> None:
        if self._mutex_handle is None or os.name != "nt":
            self._mutex_handle = None
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(self._mutex_handle)
        self._mutex_handle = None

    def close(self) -> None:
        for connection in list(self._connections):
            self._close_connection(connection)
        if self.server is not None:
            self.server.close()
            self.server = None
        self._release_mutex()

    def notify_existing(self) -> bool:
        socket = QLocalSocket()
        socket.connectToServer(self.server_name)
        if not socket.waitForConnected(300):
            return False
        written = socket.write(SHOW_MESSAGE)
        socket.flush()
        delivered = written == len(SHOW_MESSAGE) and socket.waitForBytesWritten(300)
        acknowledged = delivered and socket.waitForReadyRead(500) and bytes(socket.readAll()) == SHOW_ACK
        socket.disconnectFromServer()
        socket.waitForDisconnected(300)
        return acknowledged

    @staticmethod
    def _configure_server(server: QLocalServer) -> None:
        socket_options = getattr(QLocalServer, "SocketOption", QLocalServer)
        user_access = getattr(socket_options, "UserAccessOption", None)
        if user_access is not None:
            server.setSocketOptions(user_access)

    def _endpoint_is_active(self) -> bool:
        socket = QLocalSocket()
        socket.connectToServer(self.server_name)
        connected = socket.waitForConnected(200)
        if connected:
            socket.disconnectFromServer()
        return connected

    @staticmethod
    def _address_in_use(server: QLocalServer) -> bool:
        errors = getattr(QAbstractSocket, "SocketError", QAbstractSocket)
        address_in_use = getattr(errors, "AddressInUseError", None)
        return address_in_use is not None and server.serverError() == address_in_use

    @staticmethod
    def _schedule_connection_delete(connection) -> None:
        if connection.__dict__.get("_clipsave_delete_scheduled", False):
            return
        connection._clipsave_delete_scheduled = True
        delete_later = getattr(connection, "deleteLater", None)
        if delete_later is not None:
            delete_later()

    def _close_connection(self, connection) -> None:
        if connection not in self._connections:
            return
        self._connections.pop(connection, None)
        connection.disconnectFromServer()
        self._schedule_connection_delete(connection)

    def _accept_connection(self, connection, callback) -> None:
        if len(self._connections) >= self.MAX_CLIENTS:
            connection.disconnectFromServer()
            self._schedule_connection_delete(connection)
            return
        buffer = bytearray()
        self._connections[connection] = buffer

        def ready_read() -> None:
            if connection not in self._connections:
                return
            buffer.extend(bytes(connection.readAll()))
            if len(buffer) > len(SHOW_MESSAGE):
                self._close_connection(connection)
                return
            if len(buffer) == len(SHOW_MESSAGE):
                if _is_show_message(bytes(buffer)):
                    callback()
                    connection.write(SHOW_ACK)
                    connection.flush()
                self._close_connection(connection)

        connection.readyRead.connect(ready_read)
        def disconnected() -> None:
            self._connections.pop(connection, None)
            self._schedule_connection_delete(connection)

        connection.disconnected.connect(disconnected)
        QTimer.singleShot(self.CLIENT_TIMEOUT_MS, lambda: self._close_connection(connection))
        if connection.bytesAvailable():
            ready_read()

    def listen(self, callback) -> bool:
        if not self._acquire_mutex():
            return False
        server = QLocalServer()
        self._configure_server(server)
        if not server.listen(self.server_name):
            if not self._address_in_use(server) or self._endpoint_is_active():
                self._release_mutex()
                return False
            if not QLocalServer.removeServer(self.server_name):
                self._release_mutex()
                return False
            server = QLocalServer()
            self._configure_server(server)
            if not server.listen(self.server_name):
                self._release_mutex()
                return False
        self.server = server
        self.server.setMaxPendingConnections(self.MAX_CLIENTS)

        def incoming() -> None:
            while self.server.hasPendingConnections():
                connection = self.server.nextPendingConnection()
                if connection is None:
                    break
                self._accept_connection(connection, callback)

        self.server.newConnection.connect(incoming)
        return True


def main() -> int:
    if "--smoke-ocr-import" in sys.argv:
        from .ocr_service import WindowsOCRService

        WindowsOCRService._winrt_types()
        return 0
    if "--smoke-ocr-runtime" in sys.argv:
        import tempfile

        from PIL import Image, ImageDraw

        from .ocr_service import WindowsOCRService

        with tempfile.TemporaryDirectory(prefix="clipsave-ocr-smoke-") as temporary:
            path = Path(temporary) / "ocr-smoke.png"
            image = Image.new("RGB", (360, 100), "white")
            ImageDraw.Draw(image).text((16, 32), "ClipSave OCR 123", fill="black")
            image.save(path)
            try:
                WindowsOCRService.recognize(path)
            except RuntimeError as exc:
                if str(exc) != "Windows 没有安装可用的 OCR 语言包。":
                    raise
        return 0
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
        ensure_storage_directories()
        migration_result = migrate_legacy_layout()
        ensure_storage_directories()
    except (OSError, RuntimeError) as exc:
        QMessageBox.critical(None, "ClipSave 无法启动", str(exc))
        return 1
    try:
        database = LibraryDatabase()
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        QMessageBox.critical(None, "ClipSave 无法启动", str(exc))
        return 1
    settings = Settings()
    startup_error = None
    if smoke_profile_path is None:
        try:
            set_start_with_windows(bool(settings.get("start_with_windows", False)))
        except OSError as exc:
            startup_error = str(exc)
    window = MainWindow(
        database,
        settings,
        icon,
        scan_on_start=_should_scan_library(migration_result, database),
        reconcile_on_start=True,
    )
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

    original_excepthook = sys.excepthook
    smoke_uncaught_exceptions: list[str] = []
    if smoke_ready_path is not None:
        def smoke_excepthook(exception_type, exception, traceback) -> None:
            smoke_uncaught_exceptions.append(
                f"{exception_type.__name__}: {exception}"
            )
            original_excepthook(exception_type, exception, traceback)

        sys.excepthook = smoke_excepthook

    window.show()
    if smoke_ready_path is not None:
        smoke_attempts = 0
        smoke_status_path = smoke_ready_path.with_name(f"{smoke_ready_path.name}.status")

        def quit_smoke() -> None:
            quit_started = window.quit_application()
            try:
                smoke_status_path.write_text(
                    f"quit_returned={quit_started}\nclosing={window._closing}\n"
                    f"quit_in_progress={window._quit_in_progress}\n",
                    encoding="ascii",
                    newline="\n",
                )
            except OSError:
                pass
            if not quit_started:
                QTimer.singleShot(250, quit_smoke)

        def mark_smoke_ready() -> None:
            nonlocal smoke_attempts
            smoke_attempts += 1
            failure = _smoke_failure(window, smoke_uncaught_exceptions)
            if failure:
                try:
                    smoke_status_path.write_text(
                        failure + "\n", encoding="utf-8", newline="\n"
                    )
                except OSError:
                    pass
                app.exit(1)
                return
            try:
                check = database.connection.execute("PRAGMA quick_check").fetchone()[0]
                if (
                    window.isVisible()
                    and window._startup_scan_request is None
                    and str(check).lower() == "ok"
                ):
                    smoke_ready_path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = smoke_ready_path.with_name(f".{smoke_ready_path.name}.tmp")
                    temporary.write_text("ready\n", encoding="ascii", newline="\n")
                    os.replace(temporary, smoke_ready_path)
                    QTimer.singleShot(smoke_hold_ms, quit_smoke)
                    return
            except (OSError, sqlite3.Error):
                pass
            if smoke_attempts < 80:
                QTimer.singleShot(250, mark_smoke_ready)

        QTimer.singleShot(250, mark_smoke_ready)
    exit_code = app.exec()
    failure = _smoke_failure(window, smoke_uncaught_exceptions)
    if failure:
        exit_code = exit_code or 1
    if smoke_ready_path is not None:
        try:
            with smoke_status_path.open("a", encoding="ascii", newline="\n") as handle:
                handle.write(f"event_loop_exited={exit_code}\n")
        except OSError:
            pass
        sys.excepthook = original_excepthook
    if registered:
        _windows_hotkey_api().UnregisterHotKey(None, GLOBAL_HOTKEY_ID)
    single.close()
    return exit_code
