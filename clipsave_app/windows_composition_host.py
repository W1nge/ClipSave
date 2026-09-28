"""Win10 composition host: Acrylic and completed Qt pixels share one HWND.

The Qt widget tree stays offscreen and retains its own layout/backing store.
Only the paced frame tick resizes it and uploads changed, completed images.
Native move/resize messages never start an additional painting loop.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import logging
import os

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QContextMenuEvent, QEnterEvent, QImage, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget

from .native_window_controller import windows_resize_hit_test
from .windows_composition_input import WindowsCompositionInput
from .windows_frame import _user32, handle_getminmaxinfo, handle_nccalcsize, window_rect

log = logging.getLogger(__name__)
WNDPROC = C.WINFUNCTYPE(C.c_ssize_t, W.HWND, W.UINT, W.WPARAM, W.LPARAM)


class WindowClass(C.Structure):
    _fields_ = [('cbSize', W.UINT), ('style', W.UINT), ('lpfnWndProc', WNDPROC),
               ('cbClsExtra', C.c_int), ('cbWndExtra', C.c_int),
               ('hInstance', W.HINSTANCE), ('hIcon', W.HICON), ('hCursor', W.HANDLE),
               ('hbrBackground', W.HBRUSH), ('lpszMenuName', W.LPCWSTR),
               ('lpszClassName', W.LPCWSTR), ('hIconSm', W.HICON)]


class WindowsCompositionHost:
    def __init__(self, window, bridge):
        self.window, self.bridge = window, bridge
        self.app = QApplication.instance()
        self.user = _user32()
        signatures = {
            'RegisterClassExW': ([C.POINTER(WindowClass)], W.WORD),
            'UnregisterClassW': ([W.LPCWSTR, W.HINSTANCE], W.BOOL),
            'LoadCursorW': ([W.HINSTANCE, C.c_void_p], W.HANDLE),
            'GetClientRect': ([W.HWND, C.POINTER(W.RECT)], W.BOOL),
            'ClientToScreen': ([W.HWND, C.POINTER(W.POINT)], W.BOOL),
            'ScreenToClient': ([W.HWND, C.POINTER(W.POINT)], W.BOOL),
            'SetCapture': ([W.HWND], W.HWND), 'ReleaseCapture': ([], W.BOOL),
            'SetFocus': ([W.HWND], W.HWND), 'GetFocus': ([], W.HWND),
            'GetForegroundWindow': ([], W.HWND),
            'SetForegroundWindow': ([W.HWND], W.BOOL),
            'IsIconic': ([W.HWND], W.BOOL),
            'EnableWindow': ([W.HWND, W.BOOL], W.BOOL),
            'SendMessageW': ([W.HWND, W.UINT, W.WPARAM, W.LPARAM], C.c_ssize_t),
            'GetCursorPos': ([C.POINTER(W.POINT)], W.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.user, name)
            function.argtypes, function.restype = args, result
        kernel = C.WinDLL('kernel32', use_last_error=True)
        kernel.GetModuleHandleW.argtypes = [W.LPCWSTR]
        kernel.GetModuleHandleW.restype = W.HINSTANCE
        self.instance = kernel.GetModuleHandleW(None)
        self.hwnd = None
        self.input = None
        self.closed = self.rendering = False
        self.visible = False
        self.last_cache_key = None
        self.frame_count = 0
        self.errors = []
        self.qt_capture = self.qt_hover = None
        self.frame_timer = QTimer(window)
        self.frame_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.frame_timer.timeout.connect(self.frame_tick)
        self.callback = WNDPROC(self.wndproc)
        self.class_name = f'ClipSaveCompositionHost{os.getpid()}_{id(self)}'
        wc = WindowClass(C.sizeof(WindowClass), 8, self.callback, 0, 0, self.instance,
                         None, self.user.LoadCursorW(None, C.c_void_p(32512)), None,
                         None, self.class_name, None)
        if not self.user.RegisterClassExW(C.byref(wc)):
            raise C.WinError(C.get_last_error())
        screen = self.app.primaryScreen()
        area = screen.availableGeometry()
        scale = screen.devicePixelRatio()
        width, height = min(window.width(), area.width()), min(window.height(), area.height())
        self.hwnd = self.user.CreateWindowExW(0x00240000, self.class_name,
            window.windowTitle(), 0x800F0000,
            area.x() + round((area.width()-width)*scale/2),
            area.y() + round((area.height()-height)*scale/2),
            round(width*scale), round(height*scale), None, None, self.instance, None)
        if not self.hwnd:
            self.user.UnregisterClassW(self.class_name, self.instance)
            raise C.WinError(C.get_last_error())
        try:
            if not self.bridge.attach(int(self.hwnd), window.dark_theme):
                raise RuntimeError(f'Composition attach failed: {self.bridge.last_error}')
            self.input = WindowsCompositionInput(window, self.hwnd, self.user)
            self.update_frame_interval()
        except Exception:
            self.close()
            raise

    def show(self, command=5):
        QMainWindow.show(self.window)
        self.visible = True
        self.frame_tick()
        self.user.ShowWindow(self.hwnd, command)
        if not self.user.IsIconic(self.hwnd):
            self.frame_timer.start()

    def hide(self):
        self.visible = False
        self.frame_timer.stop()
        self.user.ShowWindow(self.hwnd, 0)
        QMainWindow.hide(self.window)

    def activate(self):
        self.show(9 if self.user.IsIconic(self.hwnd) else 5)
        self.user.SetForegroundWindow(self.hwnd)
        self.user.SetFocus(self.hwnd)
        self.input.activate()

    def toggle_maximized(self):
        self.show(9 if self.user.IsZoomed(self.hwnd) else 3)

    def start_system_move(self):
        self.qt_capture = None
        self.user.ReleaseCapture()
        point = W.POINT()
        self.user.GetCursorPos(C.byref(point))
        position = (point.x & 0xffff) | ((point.y & 0xffff) << 16)
        self.user.SendMessageW(self.hwnd, 0xA1, 2, position)  # WM_NCLBUTTONDOWN / HTCAPTION

    def update_frame_interval(self):
        bounds = window_rect(int(self.hwnd))
        if bounds is None:
            return
        center = QPoint((bounds[0]+bounds[2])//2, (bounds[1]+bounds[3])//2)
        screen = self.app.screenAt(center) or self.app.primaryScreen()
        rate = float(screen.refreshRate())
        rate = min(120.0, rate if rate >= 30 else 60.0)
        self.frame_timer.setInterval(max(1, round(1000/rate)))

    def wndproc(self, hwnd, message, wp, lp):
        try:
            if message == 0x8051:
                return int(self.bridge.has_shared_frame_clip())
            if message == 0x8052:
                return self.frame_count
            if message == 0x8053:
                return len(self.errors)
            if self.input is not None:
                result = self.input.handle(message, int(wp), int(lp))
                if result is not None:
                    return result
            if message == 0x83:
                handled, result = handle_nccalcsize(int(hwnd), int(wp), int(lp))
                if handled:
                    return result
            if message == 0x24:
                scale = max(96, self.user.GetDpiForWindow(hwnd))/96
                handled, result = handle_getminmaxinfo(int(hwnd), int(wp), int(lp),
                    (round(self.window.minimumWidth()*scale), round(self.window.minimumHeight()*scale)))
                if handled:
                    return result
            if message == 0x84:
                bounds = window_rect(int(hwnd))
                if bounds:
                    x, y = C.c_short(lp & 0xffff).value, C.c_short((lp >> 16) & 0xffff).value
                    scale = max(96, self.user.GetDpiForWindow(hwnd))/96
                    if not self.user.IsZoomed(hwnd):
                        hit = windows_resize_hit_test(x, y, *bounds, scale)
                        if hit is not None:
                            return hit
                    if y-bounds[1] < 32*scale and x < bounds[2]-138*scale:
                        return 2
                    return 1
            if message == 0x14:
                return 1
            if message in (0x200, 0x201, 0x202, 0x203, 0x204, 0x205, 0x206,
                           0x207, 0x208, 0x209, 0x20A):
                self.forward_mouse(message, wp, lp)
                return 0
            if message in (0x1F, 0x215):  # Cancel mode / capture transferred.
                self.qt_capture = None
            if message == 0x2E0:
                rect = W.RECT.from_address(lp)
                self.user.SetWindowPos(hwnd, None, rect.left, rect.top,
                    rect.right-rect.left, rect.bottom-rect.top, 0x14)
                self.update_frame_interval()
                return 0
            if message == 0x231:
                self.qt_capture = None
                self.window.native_window_controller.begin_interactive_resize()
            if message == 0x232:
                self.window.native_window_controller.end_interactive_resize()
                if self.user.GetForegroundWindow() == self.hwnd:
                    self.user.SetFocus(self.hwnd)
                    self.input.activate()
                self.frame_tick()
                self.update_frame_interval()
            if message == 5 and self.hwnd:
                minimized = wp == 1
                self.window.grid.set_preview_loading_enabled(
                    not minimized and self.window.view_stack.currentWidget() is self.window.grid)
                self.window.window_title_bar.update_maximize_state(bool(self.user.IsZoomed(hwnd)))
                if minimized:
                    self.frame_timer.stop()
                elif self.visible:
                    self.frame_timer.start()
            if message == 0x10:
                # Keep the application's notes/shutdown/close-to-tray policy.
                self.window.close()
                return 0
            if message in (0x1A, 0x218, 0x31A, 0x31E):
                self.window._schedule_material_refresh()
        except Exception as exc:
            self.errors.append(f'{type(exc).__name__}: {exc}')
            log.exception('Composition host message failed')
            return 0
        return self.user.DefWindowProcW(hwnd, message, wp, lp)

    def forward_mouse(self, message, wp, lp):
        if not self.window.isEnabled() or QApplication.activeModalWidget() is not None:
            return
        point = W.POINT(C.c_short(lp & 0xffff).value, C.c_short((lp >> 16) & 0xffff).value)
        if message == 0x20A:
            self.user.ScreenToClient(self.hwnd, C.byref(point))
        scale = max(96, self.user.GetDpiForWindow(self.hwnd))/96
        scene = QPoint(round(point.x/scale), round(point.y/scale))
        target = self.qt_capture or self.window.childAt(scene) or self.window
        local = target.mapFrom(self.window, scene)
        global_point = target.mapToGlobal(local)
        buttons = Qt.MouseButton.NoButton
        for mask, button in ((1, Qt.MouseButton.LeftButton), (2, Qt.MouseButton.RightButton), (16, Qt.MouseButton.MiddleButton)):
            if wp & mask:
                buttons |= button
        modifiers = self.input.modifiers()
        if target is not self.qt_hover:
            if self.qt_hover is not None:
                QCoreApplication.sendEvent(self.qt_hover, QEvent(QEvent.Type.Leave))
            self.qt_hover = target
            QCoreApplication.sendEvent(target, QEnterEvent(QPointF(local), QPointF(scene), QPointF(global_point)))
        if message == 0x20A:
            event = QWheelEvent(QPointF(local), QPointF(global_point), QPoint(),
                QPoint(0, C.c_short((wp >> 16) & 0xffff).value), buttons, modifiers,
                Qt.ScrollPhase.NoScrollPhase, False)
        else:
            pressed = message in (0x201, 0x204, 0x207)
            released = message in (0x202, 0x205, 0x208)
            double = message in (0x203, 0x206, 0x209)
            kind = (QEvent.Type.MouseButtonDblClick if double else QEvent.Type.MouseButtonPress if pressed
                    else QEvent.Type.MouseButtonRelease if released else QEvent.Type.MouseMove)
            button = (Qt.MouseButton.LeftButton if message in (0x201, 0x202, 0x203) else
                      Qt.MouseButton.RightButton if message in (0x204, 0x205, 0x206) else
                      Qt.MouseButton.MiddleButton if message in (0x207, 0x208, 0x209) else Qt.MouseButton.NoButton)
            if pressed or double:
                self.qt_capture = target
                self.user.SetFocus(self.hwnd)
                self.input.activate()
                self.user.SetCapture(self.hwnd)
                target.setFocus(Qt.FocusReason.MouseFocusReason)
            event = QMouseEvent(kind, QPointF(local), QPointF(scene), QPointF(global_point), button, buttons, modifiers)
            if released:
                self.qt_capture = None
                self.user.ReleaseCapture()
        QCoreApplication.sendEvent(target, event)
        if message == 0x205:
            QCoreApplication.sendEvent(target, QContextMenuEvent(
                QContextMenuEvent.Reason.Mouse, local, global_point, modifiers))
        self.input.update_position()

    def frame_tick(self):
        if self.closed or not self.visible or self.rendering or self.user.IsIconic(self.hwnd):
            return
        self.rendering = True
        try:
            rect = W.RECT()
            self.user.GetClientRect(self.hwnd, C.byref(rect))
            if rect.right < 1 or rect.bottom < 1:
                return
            scale = max(96, self.user.GetDpiForWindow(self.hwnd))/96
            # Keep mapToGlobal() correct for Qt menus/tooltips and IME queries.
            origin = W.POINT()
            self.user.ClientToScreen(self.hwnd, C.byref(origin))
            screen = self.app.screenAt(QPoint(origin.x, origin.y)) or self.window.screen()
            screen_origin = screen.geometry().topLeft()
            position = screen_origin + QPoint(round((origin.x-screen_origin.x())/scale),
                                              round((origin.y-screen_origin.y())/scale))
            if self.window.pos() != position:
                QMainWindow.move(self.window, position)
            self.window.resize(round(rect.right/scale), round(rect.bottom/scale))
            QCoreApplication.sendPostedEvents(self.window, QEvent.Type.UpdateRequest)
            # Accessing paintDevice before Qt allocates its first backing image
            # is unsafe in PySide. The first real paint arms this read.
            if not self.window._composition_painted:
                return
            image = self.window.backingStore().paintDevice()
            if not isinstance(image, QImage) or image.isNull():
                return
            if image.cacheKey() != self.last_cache_key:
                if not self.bridge.present_frame(image):
                    raise RuntimeError(f'Frame upload failed: {self.bridge.last_error}')
                self.last_cache_key = image.cacheKey()
                self.frame_count += 1
                if self.frame_count % 120 == 0:
                    self.update_frame_interval()
            self.input.update_position()
        except Exception as exc:
            self.errors.append(f'{type(exc).__name__}: {exc}')
            self.frame_timer.stop()
            log.exception('Composition presentation failed; restoring the standard window')
            QTimer.singleShot(0, self.window._fallback_from_composition)
        finally:
            self.rendering = False

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.frame_timer.stop()
        self.bridge.detach()
        if self.hwnd:
            self.user.DestroyWindow(self.hwnd)
            self.hwnd = None
        self.user.UnregisterClassW(self.class_name, self.instance)
