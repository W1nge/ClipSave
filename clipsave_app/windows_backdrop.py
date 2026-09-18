from __future__ import annotations

import ctypes
import os
import sys
import threading
from pathlib import Path


_BRIDGE_DLL = "clipsave_windows_backdrop.dll"


def _runtime_root() -> Path:
    override = os.environ.get("CLIPSAVE_WINDOWS_BACKDROP_RUNTIME")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS).resolve() / "windows_backdrop"
    return (
        Path(__file__).resolve().parents[1]
        / "build"
        / "windows_backdrop"
        / "runtime"
    )


class WindowsCompositionBackdropBridge:
    """Thin, fail-safe ctypes wrapper around the NativeAOT backdrop bridge.

    The Windows.UI.Composition objects and DispatcherQueue are created on the Qt
    GUI thread and remain thread-affine. All public operations therefore reject
    calls from a different thread after the first successful load.
    """

    RPC_E_WRONG_THREAD = 0x8001010E
    ERROR_MOD_NOT_FOUND = 126

    def __init__(self) -> None:
        self._bridge = None
        self._owner_thread_id: int | None = None
        self._attached_hwnd: int | None = None
        self._last_error: int | None = None

    @property
    def attached_hwnd(self) -> int | None:
        return self._attached_hwnd

    @property
    def last_error(self) -> int | None:
        if self._bridge is not None:
            try:
                value = int(self._bridge.clipsave_acrylic_last_error())
                return value & 0xFFFFFFFF if value else self._last_error
            except (AttributeError, OSError, TypeError, ValueError):
                pass
        return self._last_error

    @property
    def last_stage(self) -> int | None:
        if self._bridge is None:
            return None
        try:
            return int(self._bridge.clipsave_acrylic_last_stage())
        except (AttributeError, OSError, TypeError, ValueError):
            return None

    def _same_thread(self) -> bool:
        if self._owner_thread_id is None:
            return True
        if threading.get_ident() == self._owner_thread_id:
            return True
        self._last_error = self.RPC_E_WRONG_THREAD
        return False

    def _configure_exports(self, bridge) -> None:
        bridge.clipsave_acrylic_is_supported.argtypes = []
        bridge.clipsave_acrylic_is_supported.restype = ctypes.c_int
        bridge.clipsave_acrylic_attach.argtypes = [ctypes.c_void_p, ctypes.c_int]
        bridge.clipsave_acrylic_attach.restype = ctypes.c_int
        bridge.clipsave_acrylic_set_theme.argtypes = [ctypes.c_int]
        bridge.clipsave_acrylic_set_theme.restype = ctypes.c_int
        bridge.clipsave_acrylic_set_input_active.argtypes = [ctypes.c_int]
        bridge.clipsave_acrylic_set_input_active.restype = ctypes.c_int
        bridge.clipsave_acrylic_detach.argtypes = []
        bridge.clipsave_acrylic_detach.restype = ctypes.c_int
        bridge.clipsave_acrylic_last_error.argtypes = []
        bridge.clipsave_acrylic_last_error.restype = ctypes.c_int
        bridge.clipsave_acrylic_last_stage.argtypes = []
        bridge.clipsave_acrylic_last_stage.restype = ctypes.c_int

    def _ensure_loaded(self) -> bool:
        if os.name != "nt":
            return False
        if self._bridge is not None:
            return self._same_thread()

        root = _runtime_root()
        bridge_path = root / _BRIDGE_DLL
        if not bridge_path.is_file():
            self._last_error = self.ERROR_MOD_NOT_FOUND
            return False

        try:
            bridge = ctypes.CDLL(str(bridge_path))
            self._configure_exports(bridge)
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            self._last_error = (
                getattr(exc, "winerror", None)
                or getattr(exc, "errno", None)
                or self.ERROR_MOD_NOT_FOUND
            )
            return False

        self._bridge = bridge
        self._owner_thread_id = threading.get_ident()
        self._last_error = None
        return True

    def is_supported(self) -> bool:
        if not self._ensure_loaded():
            return False
        try:
            supported = bool(self._bridge.clipsave_acrylic_is_supported())
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            self._last_error = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
            return False
        if not supported:
            self._last_error = self.last_error
        return supported

    def attach(self, hwnd: int, dark: bool) -> bool:
        if not hwnd or not self.is_supported():
            return False
        try:
            applied = bool(
                self._bridge.clipsave_acrylic_attach(
                    ctypes.c_void_p(int(hwnd)),
                    1 if dark else 0,
                )
            )
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            self._last_error = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
            return False
        if applied:
            self._attached_hwnd = int(hwnd)
            self._last_error = None
            return True
        self._attached_hwnd = None
        self._last_error = self.last_error
        return False

    def set_theme(self, dark: bool) -> bool:
        if self._bridge is None or self._attached_hwnd is None:
            return True
        if not self._same_thread():
            return False
        try:
            success = bool(self._bridge.clipsave_acrylic_set_theme(1 if dark else 0))
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            self._last_error = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
            return False
        if not success:
            self._last_error = self.last_error
        return success

    def set_input_active(self, active: bool) -> bool:
        if self._bridge is None or self._attached_hwnd is None:
            return True
        if not self._same_thread():
            return False
        try:
            success = bool(
                self._bridge.clipsave_acrylic_set_input_active(1 if active else 0)
            )
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            self._last_error = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
            return False
        if not success:
            self._last_error = self.last_error
        return success

    def detach(self) -> bool:
        if self._bridge is None or self._attached_hwnd is None:
            self._attached_hwnd = None
            return True
        if not self._same_thread():
            return False
        try:
            success = bool(self._bridge.clipsave_acrylic_detach())
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            self._last_error = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
            return False
        if success:
            self._attached_hwnd = None
            self._last_error = None
        else:
            self._last_error = self.last_error
        return success


_WINDOWS_COMPOSITION_BACKDROP = WindowsCompositionBackdropBridge()


def windows_composition_backdrop_supported() -> bool:
    return _WINDOWS_COMPOSITION_BACKDROP.is_supported()


def attach_windows_composition_backdrop(hwnd: int, dark: bool) -> bool:
    return _WINDOWS_COMPOSITION_BACKDROP.attach(hwnd, dark)


def set_windows_composition_backdrop_theme(dark: bool) -> bool:
    return _WINDOWS_COMPOSITION_BACKDROP.set_theme(dark)


def set_windows_composition_backdrop_input_active(active: bool) -> bool:
    return _WINDOWS_COMPOSITION_BACKDROP.set_input_active(active)


def detach_windows_composition_backdrop() -> bool:
    return _WINDOWS_COMPOSITION_BACKDROP.detach()


def windows_composition_backdrop_error() -> int | None:
    return _WINDOWS_COMPOSITION_BACKDROP.last_error


def windows_composition_backdrop_stage() -> int | None:
    return _WINDOWS_COMPOSITION_BACKDROP.last_stage
