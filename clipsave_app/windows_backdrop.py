from __future__ import annotations

import ctypes
import os
import sys
import threading
from pathlib import Path


_BRIDGE_DLL = "clipsave_windows_backdrop.dll"
_OPTIONAL_RUNTIME_DLLS = (
    "msvcp140_app.dll",
    "vcruntime140_1_app.dll",
    "vcruntime140_app.dll",
    "Microsoft.Graphics.Canvas.dll",
)


class _ReadBuffer(ctypes.Structure):
    """CPython Py_buffer used to pin a read-only QImage memory view."""

    _fields_ = [
        ("buf", ctypes.c_void_p), ("obj", ctypes.c_void_p),
        ("length", ctypes.c_ssize_t), ("itemsize", ctypes.c_ssize_t),
        ("readonly", ctypes.c_int), ("ndim", ctypes.c_int),
        ("format", ctypes.c_char_p), ("shape", ctypes.POINTER(ctypes.c_ssize_t)),
        ("strides", ctypes.POINTER(ctypes.c_ssize_t)),
        ("suboffsets", ctypes.POINTER(ctypes.c_ssize_t)),
        ("internal", ctypes.c_void_p),
    ]


_get_read_buffer = ctypes.pythonapi.PyObject_GetBuffer
_get_read_buffer.argtypes = [ctypes.py_object, ctypes.POINTER(_ReadBuffer), ctypes.c_int]
_get_read_buffer.restype = ctypes.c_int
_release_read_buffer = ctypes.pythonapi.PyBuffer_Release
_release_read_buffer.argtypes = [ctypes.POINTER(_ReadBuffer)]
_release_read_buffer.restype = None


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
        self._runtime_modules: list[object] = []
        self._dll_directory = None
        self._owner_thread_id: int | None = None
        self._attached_hwnd: int | None = None
        self._last_error: int | None = None

    @property
    def last_error(self) -> int | None:
        if self._bridge is not None:
            try:
                value = int(self._bridge.clipsave_acrylic_last_error())
                return value & 0xFFFFFFFF if value else self._last_error
            except (AttributeError, OSError, TypeError, ValueError):
                pass
        return self._last_error

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
        bridge.clipsave_acrylic_detach.argtypes = []
        bridge.clipsave_acrylic_detach.restype = ctypes.c_int
        bridge.clipsave_acrylic_last_error.argtypes = []
        bridge.clipsave_acrylic_last_error.restype = ctypes.c_int
        # Older installed runtimes remain usable for backdrop-only rendering.
        if hasattr(bridge, "clipsave_frame_present"):
            bridge.clipsave_frame_present.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ]
            bridge.clipsave_frame_present.restype = ctypes.c_int
        if hasattr(bridge, "clipsave_frame_material"):
            bridge.clipsave_frame_material.argtypes = [ctypes.c_int, ctypes.c_int]
            bridge.clipsave_frame_material.restype = ctypes.c_int
        if hasattr(bridge, "clipsave_frame_shared_clip"):
            bridge.clipsave_frame_shared_clip.argtypes = []
            bridge.clipsave_frame_shared_clip.restype = ctypes.c_int

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
            if hasattr(os, "add_dll_directory"):
                self._dll_directory = os.add_dll_directory(str(root))
            for dll_name in _OPTIONAL_RUNTIME_DLLS:
                dll_path = root / dll_name
                if dll_path.is_file():
                    self._runtime_modules.append(ctypes.WinDLL(str(dll_path)))
            bridge = ctypes.CDLL(str(bridge_path))
            self._configure_exports(bridge)
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            self._last_error = (
                getattr(exc, "winerror", None)
                or getattr(exc, "errno", None)
                or self.ERROR_MOD_NOT_FOUND
            )
            self._runtime_modules.clear()
            if self._dll_directory is not None:
                self._dll_directory.close()
                self._dll_directory = None
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

    def supports_frames(self) -> bool:
        return bool(
            self._ensure_loaded()
            and hasattr(self._bridge, "clipsave_frame_present")
        )

    def supports_material_control(self) -> bool:
        return bool(self._ensure_loaded() and hasattr(self._bridge, "clipsave_frame_material"))

    def has_shared_frame_clip(self) -> bool:
        return bool(self._same_thread() and self._bridge is not None
                    and hasattr(self._bridge, "clipsave_frame_shared_clip")
                    and self._bridge.clipsave_frame_shared_clip())

    def set_material(self, dark: bool, enabled: bool) -> bool:
        if not self._attached_hwnd or not self.supports_material_control():
            return False
        success = bool(self._bridge.clipsave_frame_material(int(dark), int(enabled)))
        self._last_error = None if success else self.last_error
        return success

    def present_frame(self, image) -> bool:
        """Synchronously copy a completed Qt image into the composition surface.

        Keep a shared QImage reference and pin its const view. Calling bits()
        on the backing store would change its cache key; copying its mutable
        data would add a full-frame memory copy to every presentation.
        """
        from PySide6.QtGui import QImage

        if not self._attached_hwnd or not self.supports_frames():
            return False
        if (not isinstance(image, QImage) or image.isNull()
                or image.format() != QImage.Format.Format_ARGB32_Premultiplied):
            self._last_error = 87  # ERROR_INVALID_PARAMETER
            return False
        frame = QImage(image)
        view = frame.constBits()
        buffer = _ReadBuffer()
        acquired = False
        try:
            _get_read_buffer(view, ctypes.byref(buffer), 0)
            acquired = True
            success = bool(self._bridge.clipsave_frame_present(
                buffer.buf, frame.width(), frame.height(), frame.bytesPerLine(),
            ))
            self._last_error = None if success else self.last_error
            return success
        except (AttributeError, BufferError, OSError, TypeError, ValueError) as exc:
            self._last_error = getattr(exc, "winerror", None) or getattr(exc, "errno", None) or 87
            return False
        finally:
            if acquired:
                _release_read_buffer(ctypes.byref(buffer))

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


def attach_windows_composition_backdrop(hwnd: int, dark: bool) -> bool:
    return _WINDOWS_COMPOSITION_BACKDROP.attach(hwnd, dark)


def detach_windows_composition_backdrop() -> bool:
    return _WINDOWS_COMPOSITION_BACKDROP.detach()


def windows_composition_backdrop_error() -> int | None:
    return _WINDOWS_COMPOSITION_BACKDROP.last_error
