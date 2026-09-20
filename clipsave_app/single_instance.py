from __future__ import annotations

import ctypes
import hashlib
import os
from ctypes import wintypes

from PySide6.QtCore import QTimer
from PySide6.QtNetwork import QAbstractSocket, QLocalServer, QLocalSocket


SHOW_MESSAGE = b"show\n"
SHOW_ACK = b"ok\n"


def windows_user_sid() -> str:
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


def current_user_identity() -> str:
    if os.name == "nt":
        return windows_user_sid()
    if hasattr(os, "getuid"):
        return str(os.getuid())
    raise RuntimeError("Could not determine a stable current-user identity")


def instance_server_name(prefix: str) -> str:
    user_hash = hashlib.sha256(
        current_user_identity().encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:24]
    return f"{prefix}.{user_hash}"


class SingleInstance:
    MAX_CLIENTS = 16
    CLIENT_TIMEOUT_MS = 250

    def __init__(self, server_name: str | None = None):
        self.server_name = server_name or self._default_server_name()
        self.server = None
        self._mutex_handle = None
        self._connections: dict[object, bytearray] = {}

    @classmethod
    def _default_server_name(cls) -> str:
        return instance_server_name("ClipSave")

    @classmethod
    def _server_api(cls):
        return QLocalServer

    @classmethod
    def _socket_api(cls):
        return QLocalSocket

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
        if ctypes.get_last_error() == 183:
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
        socket = self._socket_api()()
        socket.connectToServer(self.server_name)
        if not socket.waitForConnected(300):
            return False
        written = socket.write(SHOW_MESSAGE)
        socket.flush()
        delivered = written == len(SHOW_MESSAGE) and socket.waitForBytesWritten(300)
        acknowledged = (
            delivered
            and socket.waitForReadyRead(500)
            and bytes(socket.readAll()) == SHOW_ACK
        )
        socket.disconnectFromServer()
        socket.waitForDisconnected(300)
        return acknowledged

    @classmethod
    def _configure_server(cls, server) -> None:
        server_api = cls._server_api()
        socket_options = getattr(server_api, "SocketOption", server_api)
        user_access = getattr(socket_options, "UserAccessOption", None)
        if user_access is not None:
            server.setSocketOptions(user_access)

    def _endpoint_is_active(self) -> bool:
        socket = self._socket_api()()
        socket.connectToServer(self.server_name)
        connected = socket.waitForConnected(200)
        if connected:
            socket.disconnectFromServer()
        return connected

    @staticmethod
    def _address_in_use(server) -> bool:
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
                if bytes(buffer) == SHOW_MESSAGE:
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
        server_api = self._server_api()
        server = server_api()
        self._configure_server(server)
        if not server.listen(self.server_name):
            if not self._address_in_use(server) or self._endpoint_is_active():
                self._release_mutex()
                return False
            if not server_api.removeServer(self.server_name):
                self._release_mutex()
                return False
            server = server_api()
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
