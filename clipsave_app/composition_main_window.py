"""Application lifecycle for the cached Win10 composition presentation path."""
from __future__ import annotations

import logging
import os
import sys

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget

from .main_window import MainWindow
from .services import (BackdropBackend, BackdropResult, _windows_backdrop_policy,
                       _windows_effect_apis, register_windows_power_saving_notification,
                       unregister_windows_power_saving_notification)
from .windows_backdrop import WindowsCompositionBackdropBridge
from .windows_frame import is_windows_qt_platform

log = logging.getLogger(__name__)


def composition_window_class(default=MainWindow):
    # Win11 retains its native DWM backdrop. An older/missing bridge also
    # keeps the existing compatibility path rather than opening a blank host.
    if not is_windows_qt_platform() or not 17763 <= sys.getwindowsversion().build < 22000:
        return default
    if os.environ.get('CLIPSAVE_COMPOSITION_HOST') == '0':
        return default
    bridge = WindowsCompositionBackdropBridge()
    if not bridge.is_supported() or not bridge.supports_frames() or not bridge.supports_material_control():
        return default
    return CompositionMainWindow


class CompositionMainWindow(MainWindow):
    def __init__(self, *args, **kwargs):
        self._composition_enabled = True
        self._composition_host = None
        self._composition_painted = False
        self._composition_power = None
        self._composition_metrics = None
        super().__init__(*args, **kwargs)
        self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self._initial_position_constrained = True
        self._clear_resize_handles()
        try:
            from .windows_composition_host import WindowsCompositionHost
            self._composition_host = WindowsCompositionHost(self, WindowsCompositionBackdropBridge())
            self._composition_power = register_windows_power_saving_notification(self._composition_host.hwnd)
            self._apply_native_backdrop(force=True)
            app = QApplication.instance()
            app.installEventFilter(self)
            app.aboutToQuit.connect(self._close_composition)
        except Exception:
            log.exception('Composition host unavailable; restoring standard presentation')
            self._fallback_from_composition(show=False)

    def _close_composition(self):
        if self._composition_power:
            unregister_windows_power_saving_notification(self._composition_power)
            self._composition_power = None
        if self._composition_host is not None:
            self._composition_metrics = (self._composition_host.frame_count, len(self._composition_host.errors))
            self._composition_host.close()
            self._composition_host = None

    def _fallback_from_composition(self, *, show=True):
        if not self._composition_enabled:
            return
        self._close_composition()
        self._composition_enabled = False
        self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, False)
        self.window_effects_controller.native_backdrop_hwnd = None
        self._ensure_native_resize_frame()
        self._ensure_power_saving_notification()
        if show:
            self.show()

    def show(self):
        if self._composition_host is not None:
            self._composition_host.show()
        else:
            super().show()

    def hide(self):
        if self._composition_host is not None:
            self._composition_host.hide()
        else:
            super().hide()

    def showMinimized(self):
        if self._composition_host is not None:
            self._composition_host.show(6)
        else:
            super().showMinimized()

    def showNormal(self):
        if self._composition_host is not None:
            self._composition_host.show(9)
        else:
            super().showNormal()

    def showMaximized(self):
        if self._composition_host is not None:
            self._composition_host.show(3)
        else:
            super().showMaximized()

    def isMinimized(self):
        host = self._composition_host
        return bool(host.user.IsIconic(host.hwnd)) if host is not None else super().isMinimized()

    def _window_is_maximized(self):
        host = self._composition_host
        return bool(host.user.IsZoomed(host.hwnd)) if host is not None else super()._window_is_maximized()

    def toggle_maximized(self):
        if self._composition_host is not None:
            self._composition_host.toggle_maximized()
        else:
            super().toggle_maximized()

    def bring_to_front(self):
        if self._composition_host is not None:
            self._composition_host.activate()
        else:
            super().bring_to_front()

    def _set_interactive_resize_active(self, active):
        if self._composition_enabled:
            self.grid.set_interactive_resize_active(active)
        else:
            super()._set_interactive_resize_active(active)

    def _ensure_native_resize_frame(self):
        if not self._composition_enabled:
            super()._ensure_native_resize_frame()

    def _ensure_power_saving_notification(self):
        if not self._composition_enabled:
            super()._ensure_power_saving_notification()

    def _sync_windows_backdrop_geometry_now(self):
        if not self._composition_enabled:
            super()._sync_windows_backdrop_geometry_now()

    def _sync_windows_backdrop_window(self, **kwargs):
        if not self._composition_enabled:
            super()._sync_windows_backdrop_window(**kwargs)

    def _apply_native_backdrop(self, *, force=False, dark=None):
        if not self._composition_enabled:
            return super()._apply_native_backdrop(force=force, dark=dark)
        host = self._composition_host
        if host is None:
            return
        user, _ = _windows_effect_apis()
        enabled = _windows_backdrop_policy(user).allows_app_managed_backdrop
        applied = host.bridge.set_material(self.dark_theme if dark is None else dark, enabled)
        if not applied:
            raise RuntimeError(f'Composition material failed: {host.bridge.last_error}')
        result = BackdropResult(BackdropBackend.WIN10_EFFECT_ACRYLIC if enabled else BackdropBackend.SOLID, True)
        self.window_effects_controller.native_backdrop_hwnd = host.hwnd
        self.window_effects_controller.native_backdrop_result = result
        self._sync_surface_style(result=result, dark=dark)

    def showEvent(self, event):
        if not self._composition_enabled:
            return super().showEvent(event)
        QMainWindow.showEvent(self, event)
        self.grid.set_preview_loading_enabled(self.view_stack.currentWidget() is self.grid)

    def paintEvent(self, event):
        super().paintEvent(event)
        self._composition_painted = True

    def nativeEvent(self, event_type, message):
        if not self._composition_enabled:
            return super().nativeEvent(event_type, message)
        return QMainWindow.nativeEvent(self, event_type, message)

    def eventFilter(self, watched, event):
        host = self._composition_host
        if host is not None and not host.closed:
            if watched is self and event.type() in (QEvent.Type.WindowBlocked, QEvent.Type.WindowUnblocked):
                host.user.EnableWindow(host.hwnd, event.type() == QEvent.Type.WindowUnblocked)
            if event.type() == QEvent.Type.Show and isinstance(watched, QWidget) and watched.isWindow() and watched is not self:
                parent = watched.parentWidget()
                if parent is not None and (parent is self or self.isAncestorOf(parent)):
                    # Give native Qt dialogs/popups the visible owner for taskbar,
                    # z-order, minimize and modal behavior. Their Qt input stays native.
                    host.user.SetWindowLongPtrW(int(watched.winId()), -8, host.hwnd)
        return super().eventFilter(watched, event)
