from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, QByteArray
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from .constants import APP_NAME, INSTANCE_SERVER
from .database import LibraryDatabase
from .main_window import MainWindow
from .settings import Settings
from .storage import migrate_legacy_layout


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
    def __init__(self):
        self.server = None

    def notify_existing(self) -> bool:
        socket = QLocalSocket()
        socket.connectToServer(INSTANCE_SERVER)
        if socket.waitForConnected(300):
            socket.write(b"show")
            socket.flush()
            socket.waitForBytesWritten(300)
            socket.disconnectFromServer()
            return True
        QLocalServer.removeServer(INSTANCE_SERVER)
        return False

    def listen(self, callback) -> None:
        self.server = QLocalServer()
        self.server.listen(INSTANCE_SERVER)

        def incoming() -> None:
            connection = self.server.nextPendingConnection()
            connection.waitForReadyRead(100)
            connection.readAll()
            callback()
            connection.disconnectFromServer()

        self.server.newConnection.connect(incoming)


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

    migrate_legacy_layout()
    database = LibraryDatabase()
    settings = Settings()
    window = MainWindow(database, settings, icon)
    single.listen(window.bring_to_front)

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
