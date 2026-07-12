from __future__ import annotations

import ctypes
import getpass
import hashlib
import hmac
import os
import sqlite3
import sys
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QAbstractNativeEventFilter, QByteArray
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtNetwork import QAbstractSocket, QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox

from .constants import APP_NAME, INSTANCE_SERVER
from .database import LibraryDatabase
from .main_window import MainWindow
from .settings import Settings
from .storage import ensure_storage_directories, migrate_legacy_layout


SHOW_MESSAGE = b"show\n"


def _current_user_identity() -> str:
    if hasattr(os, "getuid"):
        return str(os.getuid())
    return "|".join(
        (
            os.environ.get("USERDOMAIN", ""),
            getpass.getuser(),
            os.path.normcase(str(Path.home())),
        )
    )


def _instance_server_name() -> str:
    user_hash = hashlib.sha256(_current_user_identity().encode("utf-8", errors="surrogatepass")).hexdigest()[:24]
    return f"{INSTANCE_SERVER}.{user_hash}"


def _is_show_message(message: bytes) -> bool:
    return hmac.compare_digest(message, SHOW_MESSAGE)


def _migration_moved_files(result: dict[str, int]) -> bool:
    return any(type(count) is int and count > 0 for count in result.values())


def _should_scan_library(migration_result: dict[str, int], database) -> bool:
    if getattr(database, "needs_library_rescan", False) or _migration_moved_files(migration_result):
        return True
    try:
        row = database.connection.execute("SELECT 1 FROM items WHERE missing = 0 LIMIT 1").fetchone()
    except (AttributeError, sqlite3.Error):
        return False
    return row is None


def create_app_icon() -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#2f7df6"))
    painter.setPen(QColor("#2f7df6"))
    painter.drawRoundedRect(5, 7, 49, 49, 12, 12)
    painter.setBrush(QColor("#ffffff"))
    painter.setPen(QColor("#ffffff"))
    painter.drawRoundedRect(17, 17, 31, 31, 8, 8)
    painter.setBrush(QColor("#2f7df6"))
    painter.drawRoundedRect(25, 25, 15, 15, 4, 4)
    painter.end()
    return QIcon(pixmap)


class GlobalHotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def nativeEventFilter(self, event_type: QByteArray, message):
        if os.name == "nt" and event_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            try:
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == 0x0312 and msg.wParam == 0xC51A:
                    self.callback()
                    return True, 0
            except (TypeError, ValueError):
                pass
        return False, 0


class SingleInstance:
    def __init__(self, server_name: str | None = None):
        self.server_name = server_name or _instance_server_name()
        self.server = None

    def notify_existing(self) -> bool:
        socket = QLocalSocket()
        socket.connectToServer(self.server_name)
        if not socket.waitForConnected(300):
            return False
        written = socket.write(SHOW_MESSAGE)
        socket.flush()
        delivered = written == len(SHOW_MESSAGE) and socket.waitForBytesWritten(300)
        socket.disconnectFromServer()
        return delivered

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
    def _read_message(connection) -> bytes:
        message = bytearray()
        while len(message) <= len(SHOW_MESSAGE):
            if connection.bytesAvailable() == 0 and not connection.waitForReadyRead(100):
                break
            chunk = bytes(connection.readAll())
            if not chunk:
                break
            message.extend(chunk)
            if len(message) >= len(SHOW_MESSAGE):
                if connection.waitForReadyRead(10):
                    continue
                break
        return bytes(message)

    def listen(self, callback) -> bool:
        server = QLocalServer()
        self._configure_server(server)
        if not server.listen(self.server_name):
            if not self._address_in_use(server) or self._endpoint_is_active():
                return False
            if not QLocalServer.removeServer(self.server_name):
                return False
            server = QLocalServer()
            self._configure_server(server)
            if not server.listen(self.server_name):
                return False
        self.server = server

        def incoming() -> None:
            while self.server.hasPendingConnections():
                connection = self.server.nextPendingConnection()
                if connection is None:
                    break
                if _is_show_message(self._read_message(connection)):
                    callback()
                connection.disconnectFromServer()

        self.server.newConnection.connect(incoming)
        return True


def main() -> int:
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    icon = create_app_icon()
    app.setWindowIcon(icon)

    single = SingleInstance()
    if single.notify_existing():
        return 0
    window_holder: list[MainWindow] = []

    def show_window() -> None:
        if window_holder:
            window_holder[0].bring_to_front()

    if not single.listen(show_window):
        if single.notify_existing():
            return 0
        return 1

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
    window = MainWindow(
        database,
        settings,
        icon,
        scan_on_start=_should_scan_library(migration_result, database),
        reconcile_on_start=True,
    )
    window_holder.append(window)

    hotkey_filter = GlobalHotkeyFilter(window.focus_search)
    app.installNativeEventFilter(hotkey_filter)
    registered = False
    if os.name == "nt":
        registered = bool(ctypes.windll.user32.RegisterHotKey(None, 0xC51A, 0x0002 | 0x0001, ord("V")))

    window.show()
    exit_code = app.exec()
    if registered:
        ctypes.windll.user32.UnregisterHotKey(None, 0xC51A)
    return exit_code
