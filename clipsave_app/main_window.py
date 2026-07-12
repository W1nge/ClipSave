from __future__ import annotations

import ctypes
import json
import os
import threading
import time
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QIcon, QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedLayout,
    QStackedWidget,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from send2trash import send2trash

from .constants import APP_NAME, LIBRARY_DIR, MAX_IMPORT_BYTES, MAX_MARKDOWN_BYTES
from .database import ImportFileResult, LibraryDatabase
from .services import (
    AIService,
    ClipboardService,
    OperationCancelled,
    TaskCapacityExceeded,
    ai_ocr_task_executor,
    apply_windows_acrylic,
    preflight_image_file,
    shutdown_ai_ocr_task_executor,
)
from .ocr_service import WindowsOCRService
from .settings import Settings
from .storage import is_under_local_store, recycle_managed_file
from .styles import DARK_STYLESHEET, LIGHT_STYLESHEET
from .windows_frame import (
    WM_NCCALCSIZE,
    enable_native_resize_frame,
    handle_nccalcsize,
    is_windows_qt_platform,
    window_dpi_scale,
    window_rect,
)
from .widgets import (
    AssetGrid,
    AssetTable,
    BrandLabel,
    CaptureStatusButton,
    DateDialog,
    DetailPanel,
    DraggableBar,
    IconButton,
    MarkdownDialog,
    SettingsDialog,
    ResizeHandle,
    Sidebar,
    WindowTitleBar,
    lucide_icon,
)


SORT_BUTTON_LABELS = {
    "newest": "排序：最新",
    "oldest": "排序：最早",
    "name": "排序：名称",
    "size": "排序：大小",
    "type": "排序：类型",
}


def system_uses_dark_theme() -> bool:
    scheme = QApplication.styleHints().colorScheme()
    if scheme == Qt.ColorScheme.Dark:
        return True
    if scheme == Qt.ColorScheme.Light:
        return False
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ) as key:
                return int(winreg.QueryValueEx(key, "AppsUseLightTheme")[0]) == 0
        except (OSError, ValueError):
            pass
    return False


class AsyncSignals(QObject):
    succeeded = Signal(int, str, object)
    failed = Signal(str)


class MainWindow(QMainWindow):
    RESIZE_EDGE_WIDTH = 8
    RESIZE_CORNER_SIZE = 14

    def __init__(
        self,
        database: LibraryDatabase,
        settings: Settings,
        app_icon: QIcon,
        scan_on_start: bool = True,
        reconcile_on_start: bool = False,
    ):
        super().__init__()
        self.database = database
        self.settings = settings
        self.app_icon = app_icon
        self.current_items = []
        self.current_item_id: int | None = None
        self.current_kind: str | None = None
        self.current_favorite = False
        self.current_day: str | None = None
        self.current_recent = False
        self.current_collection: int | None = None
        self.current_tag: int | None = None
        self.current_sort = settings.get("sort", "newest")
        self.sort_menu = None
        self.sort_menu_closed_at = 0.0
        self.force_quit = False
        self._grid_dirty = True
        self._table_dirty = True
        self._async_signals: set[AsyncSignals] = set()
        self._ai_requests: dict[int, tuple[object, AsyncSignals]] = {}
        self._ocr_requests: dict[int, tuple[object, AsyncSignals]] = {}
        self._semantic_request: tuple[object, AsyncSignals] | None = None
        self._semantic_results_active = False
        self._semantic_ordered_ids: list[int] = []
        self._session_hidden_item_ids: set[int] = set()
        self._startup_scan_request: tuple[object, AsyncSignals] | None = None
        self.startup_scan_error: str | None = None
        self._import_request: tuple[object, AsyncSignals] | None = None
        self._copy_request: tuple[object, AsyncSignals, int] | None = None
        self._backup_request: tuple[object, AsyncSignals] | None = None
        self._async_tasks: dict[object, tuple[threading.Event, threading.Thread]] = {}
        self._bounded_tasks: dict[object, object] = {}
        self._async_tasks_lock = threading.Lock()
        self._closing = False
        self._quit_in_progress = False
        self._interactive_resize_active = False
        self._native_resize_frame_enabled = False
        self._native_resize_frame_hwnd: int | None = None
        self._initial_position_constrained = False
        self.global_hotkey_registered: bool | None = None
        self.dark_theme = self._desired_dark_theme()
        app = QApplication.instance()
        if app is not None:
            app.setProperty("darkTheme", self.dark_theme)

        self.setWindowTitle(APP_NAME)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.resize(1440, 880)
        self.setMinimumSize(800, 440)
        self.setStyleSheet(DARK_STYLESHEET if self.dark_theme else LIGHT_STYLESHEET)
        self.build_ui()
        self.build_tray()
        self.build_shortcuts()
        self._ensure_native_resize_frame()
        color_scheme_changed = getattr(QApplication.styleHints(), "colorSchemeChanged", None)
        if color_scheme_changed is not None:
            color_scheme_changed.connect(self._system_color_scheme_changed)

        self.clipboard_service = ClipboardService(database, self)
        self.clipboard_service.captured.connect(self.on_captured)
        self.clipboard_service.failed.connect(self.show_error_status)
        self.clipboard_service.state_changed.connect(self.update_monitor_button)
        if settings.get("monitoring", False):
            self.clipboard_service.start()
        else:
            self.update_monitor_button(False)

        self.refresh_library()
        if scan_on_start or reconcile_on_start:
            self._start_startup_scan(scan_on_start, reconcile_on_start)
        self.backup_timer = QTimer(self)
        self.backup_timer.setInterval(5 * 60 * 1000)
        self.backup_timer.timeout.connect(self._start_periodic_backup)
        self.backup_timer.start()
        QTimer.singleShot(0, self._show_database_recovery_state)
        QTimer.singleShot(100, lambda: apply_windows_acrylic(self, self.dark_theme))

    def _desired_dark_theme(self) -> bool:
        if self.settings.get("follow_system_theme", True):
            return system_uses_dark_theme()
        return self.settings.get("theme_mode", "light") == "dark"

    def _system_color_scheme_changed(self, _scheme) -> None:
        if self.settings.get("follow_system_theme", True):
            self.apply_theme()

    def apply_theme(self, force: bool = False) -> None:
        dark = self._desired_dark_theme()
        if not force and dark == self.dark_theme:
            return
        self.dark_theme = dark
        app = QApplication.instance()
        if app is not None:
            app.setProperty("darkTheme", dark)
        self.setStyleSheet(DARK_STYLESHEET if dark else LIGHT_STYLESHEET)
        for button in self.findChildren(IconButton):
            button.refresh_theme()
        self.window_title_bar.update_maximize_state(self.isMaximized())
        self.sidebar.set_active(getattr(self.sidebar, "active_key", ""))
        self.semantic_button.setIcon(lucide_icon("sparkles"))
        self.detail.ai_button.setIcon(lucide_icon("sparkles"))
        self.detail.ocr_button.setIcon(lucide_icon("scan-text"))
        self.grid.viewport().update()
        self.table.viewport().update()
        QTimer.singleShot(0, lambda: apply_windows_acrylic(self, dark))

    def build_ui(self) -> None:
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(220)
        self.search_timer.timeout.connect(self.refresh_items)
        root = QWidget()
        root.setObjectName("AppRoot")
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.window_title_bar = WindowTitleBar()
        self.window_title_bar.minimize_button.clicked.connect(self.showMinimized)
        self.window_title_bar.maximize_button.clicked.connect(self.toggle_maximized)
        self.window_title_bar.close_button.clicked.connect(self.close)
        root_layout.addWidget(self.window_title_bar)

        body = QWidget()
        body.setObjectName("WindowBody")
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        root_layout.addWidget(body, 1)

        self.brand_label = BrandLabel("ClipSave", "#21a8fb", 0.86, root)
        self.brand_label.setGeometry(10, 0, 190, 86)
        self.brand_label.raise_()

        self.sidebar = Sidebar()
        self.sidebar.navigation_requested.connect(self.navigate)
        self.sidebar.add_collection_requested.connect(self.add_collection)
        self.sidebar.add_tag_requested.connect(self.add_global_tag)
        self.sidebar.settings_requested.connect(self.open_settings)
        self.sidebar.collapsed_changed.connect(lambda value: self._save_setting("sidebar_collapsed", value))
        self.sidebar.width_animation_started.connect(self._begin_sidebar_animation)
        self.sidebar.width_animation_finished.connect(self._end_sidebar_animation)
        body_layout.addWidget(self.sidebar)

        middle = QWidget()
        middle.setObjectName("ContentSurface")
        middle_layout = QVBoxLayout(middle)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        middle_layout.setSpacing(0)
        body_layout.addWidget(middle, 1)

        top_bar = DraggableBar()
        top_bar.setObjectName("TopBar")
        top_bar.setFixedHeight(Sidebar.BRAND_AREA_HEIGHT)
        self.top_bar = top_bar
        top_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(16, 10, 16, 10)
        top_layout.setSpacing(8)
        top_layout.addStretch(1)
        self.search = __import__("PySide6.QtWidgets", fromlist=["QLineEdit"]).QLineEdit()
        self.search.setPlaceholderText("搜索剪贴板内容、文件名、标签、OCR 或 AI 描述  (Ctrl+K)")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(560)
        self.search.setMinimumWidth(160)
        self.search.textChanged.connect(lambda _text: self.search_timer.start())
        top_layout.addWidget(self.search, 3)
        self.semantic_button = QPushButton("语义搜索")
        self.semantic_button.setIcon(lucide_icon("sparkles"))
        self.semantic_button.setToolTip("使用已生成的图片向量按含义搜索")
        self.semantic_button.clicked.connect(self.semantic_search)
        top_layout.addWidget(self.semantic_button)
        top_layout.addStretch(1)
        self.sort_button = QPushButton(
            SORT_BUTTON_LABELS.get(self.current_sort, SORT_BUTTON_LABELS["newest"]) + "  ▾"
        )
        self.sort_button.clicked.connect(self.open_sort_menu)
        top_layout.addWidget(self.sort_button)
        self.grid_button = IconButton("grid", "网格视图")
        self.grid_button.clicked.connect(lambda: self.set_view_mode("grid"))
        top_layout.addWidget(self.grid_button)
        self.list_button = IconButton("list", "列表视图")
        self.list_button.clicked.connect(lambda: self.set_view_mode("list"))
        top_layout.addWidget(self.list_button)
        self.detail_button = IconButton("info", "显示详情")
        self.detail_button.clicked.connect(self.toggle_detail)
        top_layout.addWidget(self.detail_button)
        self.capture_status = CaptureStatusButton()
        self.capture_status.clicked.connect(self.toggle_monitor)
        top_layout.addWidget(self.capture_status)
        middle_layout.addWidget(top_bar)

        title_bar = QFrame()
        title_bar.setObjectName("LibraryHeader")
        title_bar.setFixedHeight(44)
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(20, 12, 20, 6)
        self.page_title = QLabel("全部内容")
        self.page_title.setObjectName("SectionTitle")
        title_layout.addWidget(self.page_title)
        self.result_count = QLabel()
        self.result_count.setObjectName("Muted")
        title_layout.addWidget(self.result_count)
        title_layout.addStretch()
        self.filter_hint = QLabel()
        self.filter_hint.setObjectName("Muted")
        title_layout.addWidget(self.filter_hint)
        content_row = QHBoxLayout()
        content_row.setContentsMargins(0, 0, 0, 0)
        content_row.setSpacing(0)
        self.view_stack = QStackedWidget()
        self.view_stack.setObjectName("ViewStack")
        self.grid = AssetGrid()
        self.grid.item_selected.connect(self.select_item)
        self.grid.selection_cleared.connect(self.clear_item_selection)
        self.grid.item_activated.connect(self.activate_item)
        self.grid.favorite_requested.connect(self.set_favorite)
        self.table = AssetTable()
        self.table.item_selected.connect(self.select_item)
        self.table.selection_cleared.connect(self.clear_item_selection)
        self.table.item_activated.connect(self.activate_item)
        self.table.favorite_requested.connect(self.set_favorite)
        self.table_page = QWidget()
        table_page_layout = QVBoxLayout(self.table_page)
        table_page_layout.setContentsMargins(0, 44, 0, 0)
        table_page_layout.setSpacing(0)
        table_page_layout.addWidget(self.table)
        self.view_stack.addWidget(self.grid)
        self.view_stack.addWidget(self.table_page)

        self.library_surface = QWidget()
        library_layers = QStackedLayout(self.library_surface)
        library_layers.setContentsMargins(0, 0, 0, 0)
        library_layers.setStackingMode(QStackedLayout.StackingMode.StackAll)
        library_layers.addWidget(self.view_stack)
        header_overlay = QWidget()
        header_overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        header_overlay_layout = QVBoxLayout(header_overlay)
        header_overlay_layout.setContentsMargins(0, 0, 14, 0)
        header_overlay_layout.setSpacing(0)
        header_overlay_layout.addWidget(title_bar)
        header_overlay_layout.addStretch(1)
        library_layers.addWidget(header_overlay)
        library_layers.setCurrentWidget(header_overlay)
        self.library_header = title_bar
        content_row.addWidget(self.library_surface, 1)
        self.detail = DetailPanel()
        self.detail.setVisible(False)
        self.detail.close_requested.connect(self.hide_detail)
        self.detail.copy_requested.connect(self.copy_item)
        self.detail.open_requested.connect(self.activate_item)
        self.detail.delete_requested.connect(self.delete_item)
        self.detail.favorite_requested.connect(self.set_favorite)
        self.detail.notes_changed.connect(self.save_notes)
        self.detail.add_tag_requested.connect(self.add_tag_to_item)
        self.detail.remove_tag_requested.connect(self.remove_tag_from_item)
        self.detail.collection_changed.connect(self.set_item_collection)
        self.detail.ai_requested.connect(self.generate_ai_description)
        self.detail.ocr_requested.connect(self.generate_ocr)
        content_row.addWidget(self.detail)
        middle_layout.addLayout(content_row, 1)

        self.set_view_mode(self.settings.get("view_mode", "grid"))
        self.sidebar.set_collapsed(bool(self.settings.get("sidebar_collapsed", False)), animate=False)
        self._create_resize_handles(root)

    def _create_resize_handles(self, parent) -> None:
        if os.name == "nt":
            self.resize_handles = {}
            return
        self._install_resize_handles(parent)

    def _install_resize_handles(self, parent) -> None:
        self.resize_handles = {
            "left": ResizeHandle(Qt.Edge.LeftEdge, Qt.CursorShape.SizeHorCursor, parent),
            "right": ResizeHandle(Qt.Edge.RightEdge, Qt.CursorShape.SizeHorCursor, parent),
            "top": ResizeHandle(Qt.Edge.TopEdge, Qt.CursorShape.SizeVerCursor, parent),
            "bottom": ResizeHandle(Qt.Edge.BottomEdge, Qt.CursorShape.SizeVerCursor, parent),
            "top_left": ResizeHandle(Qt.Edge.TopEdge | Qt.Edge.LeftEdge, Qt.CursorShape.SizeFDiagCursor, parent),
            "top_right": ResizeHandle(Qt.Edge.TopEdge | Qt.Edge.RightEdge, Qt.CursorShape.SizeBDiagCursor, parent),
            "bottom_left": ResizeHandle(Qt.Edge.BottomEdge | Qt.Edge.LeftEdge, Qt.CursorShape.SizeBDiagCursor, parent),
            "bottom_right": ResizeHandle(Qt.Edge.BottomEdge | Qt.Edge.RightEdge, Qt.CursorShape.SizeFDiagCursor, parent),
        }
        self._update_resize_handles()

    def _ensure_native_resize_frame(self) -> None:
        if not is_windows_qt_platform():
            return
        hwnd = int(self.winId())
        if self._native_resize_frame_enabled and self._native_resize_frame_hwnd == hwnd:
            return
        enabled = enable_native_resize_frame(hwnd)
        self._native_resize_frame_enabled = enabled
        self._native_resize_frame_hwnd = hwnd if enabled else None
        if enabled:
            if self.resize_handles:
                for handle in self.resize_handles.values():
                    handle.deleteLater()
                self.resize_handles = {}
            return
        if not self.resize_handles:
            self._install_resize_handles(self.centralWidget())

    def _update_resize_handles(self) -> None:
        if not getattr(self, "resize_handles", None):
            return
        hidden = self.isMaximized() or self.isFullScreen()
        for handle in self.resize_handles.values():
            handle.setVisible(not hidden)
        if hidden:
            return
        width, height = self.width(), self.height()
        edge, corner = self.RESIZE_EDGE_WIDTH, self.RESIZE_CORNER_SIZE
        geometries = {
            "left": (0, corner, edge, max(0, height - 2 * corner)),
            "right": (width - edge, corner, edge, max(0, height - 2 * corner)),
            "top": (corner, 0, max(0, width - 2 * corner), edge),
            "bottom": (corner, height - edge, max(0, width - 2 * corner), edge),
            "top_left": (0, 0, corner, corner),
            "top_right": (width - corner, 0, corner, corner),
            "bottom_left": (0, height - corner, corner, corner),
            "bottom_right": (width - corner, height - corner, corner, corner),
        }
        for key, geometry in geometries.items():
            self.resize_handles[key].setGeometry(*geometry)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_resize_handles()

    def toggle_maximized(self) -> None:
        self.showNormal() if self.isMaximized() else self.showMaximized()
        self.window_title_bar.update_maximize_state(self.isMaximized())

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self, "window_title_bar"):
            self.window_title_bar.update_maximize_state(self.isMaximized())
            self._update_resize_handles()
        super().changeEvent(event)

    def nativeEvent(self, event_type, message):
        if os.name == "nt" and event_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_NCCALCSIZE:
                handled, result = handle_nccalcsize(int(msg.hWnd), int(msg.wParam), int(msg.lParam))
                if handled:
                    return True, result
            if msg.message == 0x0231:  # WM_ENTERSIZEMOVE
                self._begin_interactive_resize()
            elif msg.message == 0x0232:  # WM_EXITSIZEMOVE
                self._end_interactive_resize()
            if msg.message == 0x0084 and not (self.isMaximized() or self.isFullScreen()):  # WM_NCHITTEST
                hwnd = int(msg.hWnd) or int(self.winId())
                rect = window_rect(hwnd)
                if rect is None:
                    return super().nativeEvent(event_type, message)
                x = ctypes.c_short(msg.lParam & 0xFFFF).value
                y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
                hit = self._windows_resize_hit_test(
                    x,
                    y,
                    *rect,
                    window_dpi_scale(hwnd),
                )
                if hit is not None:
                    return True, hit
        return super().nativeEvent(event_type, message)

    @classmethod
    def _windows_resize_hit_test(
        cls,
        x: int,
        y: int,
        left: int,
        top: int,
        right: int,
        bottom: int,
        device_pixel_ratio: float,
    ) -> int | None:
        if x < left or x >= right or y < top or y >= bottom:
            return None
        edge = max(1, round(cls.RESIZE_EDGE_WIDTH * device_pixel_ratio))
        corner = max(edge, round(cls.RESIZE_CORNER_SIZE * device_pixel_ratio))
        on_left = x < left + edge
        on_right = x >= right - edge
        on_top = y < top + edge
        on_bottom = y >= bottom - edge
        near_left = x < left + corner
        near_right = x >= right - corner
        near_top = y < top + corner
        near_bottom = y >= bottom - corner
        if (on_top and near_left) or (on_left and near_top):
            return 13  # HTTOPLEFT
        if (on_top and near_right) or (on_right and near_top):
            return 14  # HTTOPRIGHT
        if (on_bottom and near_left) or (on_left and near_bottom):
            return 16  # HTBOTTOMLEFT
        if (on_bottom and near_right) or (on_right and near_bottom):
            return 17  # HTBOTTOMRIGHT
        if on_left:
            return 10  # HTLEFT
        if on_right:
            return 11  # HTRIGHT
        if on_top:
            return 12  # HTTOP
        if on_bottom:
            return 15  # HTBOTTOM
        return None

    def _begin_interactive_resize(self) -> None:
        if self._interactive_resize_active:
            return
        self._interactive_resize_active = True
        self.grid.set_layout_updates_suspended(True)

    def _end_interactive_resize(self) -> None:
        if not self._interactive_resize_active:
            return
        self._interactive_resize_active = False
        self.grid.set_layout_updates_suspended(False)

    def _begin_sidebar_animation(self) -> None:
        self.grid.set_layout_updates_suspended(True)
        self.table.setUpdatesEnabled(False)

    def _end_sidebar_animation(self) -> None:
        self.grid.set_layout_updates_suspended(False)
        self.table.setUpdatesEnabled(True)
        self.table.viewport().update()

    def build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self.app_icon, self)
        self.tray.setToolTip("ClipSave - 正在监听剪贴板")
        menu = QMenu()
        self.tray_show_action = menu.addAction("显示 ClipSave")
        self.tray_show_action.triggered.connect(self.bring_to_front)
        self.tray_monitor_action = menu.addAction("暂停监听")
        self.tray_monitor_action.triggered.connect(self.toggle_monitor)
        menu.addSeparator()
        self.tray_quit_action = menu.addAction("退出")
        self.tray_quit_action.triggered.connect(self.quit_application)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.bring_to_front() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        self.tray.show()

    def build_shortcuts(self) -> None:
        self.shortcuts = [
            QShortcut(QKeySequence("Ctrl+K"), self, activated=self.focus_search),
            QShortcut(QKeySequence("Ctrl+F"), self, activated=self.focus_search),
            QShortcut(QKeySequence("Ctrl+B"), self, activated=self.sidebar.toggle_collapsed),
            QShortcut(QKeySequence("Ctrl+I"), self, activated=self.toggle_detail),
            QShortcut(QKeySequence("Delete"), self, activated=lambda: self.current_item_id and self.delete_item(self.current_item_id)),
            QShortcut(QKeySequence("Ctrl+C"), self, activated=self._copy_focused_selection_or_item),
        ]

    def _copy_focused_selection_or_item(self) -> None:
        focused = QApplication.focusWidget()
        if isinstance(focused, (QLineEdit, QTextEdit)):
            if (isinstance(focused, QLineEdit) and focused.hasSelectedText()) or (
                isinstance(focused, QTextEdit) and focused.textCursor().hasSelection()
            ):
                focused.copy()
                return
        if isinstance(focused, QLabel) and focused.hasSelectedText():
            QApplication.clipboard().setText(focused.selectedText())
            return
        if self.current_item_id:
            self.copy_item(self.current_item_id)

    def _set_interactions_enabled(self, enabled: bool) -> None:
        self.centralWidget().setEnabled(enabled)
        for shortcut in getattr(self, "shortcuts", []):
            shortcut.setEnabled(enabled)
        for action_name in ("tray_show_action", "tray_monitor_action", "tray_quit_action"):
            action = getattr(self, action_name, None)
            if action is not None:
                action.setEnabled(enabled)

    @staticmethod
    def _exec_transient_dialog(dialog) -> int:
        try:
            return dialog.exec()
        finally:
            dialog.deleteLater()

    def refresh_library(self) -> None:
        self._refresh_navigation_metadata()
        self.refresh_items()

    def _refresh_navigation_metadata(self) -> None:
        counts = self.database.counts()
        self.sidebar.set_primary(counts)
        self.sidebar.set_collections(self.database.collections())
        self.sidebar.set_tags(self.database.tags())
        self.detail.set_collections(self.database.collections())

    def refresh_items(self) -> None:
        self._semantic_results_active = False
        self._cancel_semantic_request()
        items = self.database.query_items(
            query=self.search.text().strip(),
            kind=self.current_kind,
            favorite=self.current_favorite,
            day=self.current_day,
            recent_days=7 if self.current_recent else None,
            collection_id=self.current_collection,
            tag_id=self.current_tag,
            sort=self.current_sort,
            summary_only=True,
        )
        self._apply_items(items)
        self.result_count.setText(f"{len(self.current_items):,} 项")
        filters = []
        if self.current_day:
            filters.append(self.current_day)
        if self.search.text().strip():
            filters.append(f"搜索：{self.search.text().strip()}")
        self.filter_hint.setText("  ·  ".join(filters))

    def _apply_items(self, items) -> None:
        self.current_items = [
            item for item in items if item["id"] not in self._session_hidden_item_ids
        ]
        visible_ids = {item["id"] for item in self.current_items}
        if self.current_item_id is not None and self.current_item_id not in visible_ids:
            self.current_item_id = None
            self.grid.clear_selection()
            self.table.clear_selected_item()
            self.detail.clear_item()
        self._grid_dirty = True
        self._table_dirty = True
        self._refresh_visible_view()

    def _refresh_visible_view(self) -> None:
        if self.view_stack.currentWidget() is self.grid:
            if self._grid_dirty:
                self.grid.set_items(self.current_items, self.current_item_id)
                self._grid_dirty = False
        elif self._table_dirty:
            self.table.set_items(self.current_items, self.current_item_id)
            self._table_dirty = False

    def navigate(self, key: str, value) -> None:
        if key == "date":
            dialog = DateDialog(self.database.days(), self)
            dialog.day_selected.connect(self.open_day)
            self._exec_transient_dialog(dialog)
            return
        self.current_kind = None
        self.current_favorite = False
        self.current_day = None
        self.current_recent = False
        self.current_collection = None
        self.current_tag = None
        titles = {"all": "全部内容", "favorite": "收藏", "recent": "最近使用", "image": "图片", "text": "文字", "markdown": "Markdown"}
        if key in ("image", "text", "markdown"):
            self.current_kind = key
        elif key == "favorite":
            self.current_favorite = True
        elif key == "recent":
            self.current_recent = True
        elif key == "collection":
            self.current_collection = int(value)
            row = next((row for row in self.database.collections() if row["id"] == value), None)
            titles[key] = row["name"] if row else "集合"
        elif key == "tag":
            self.current_tag = int(value)
            row = next((row for row in self.database.tags() if row["id"] == value), None)
            titles[key] = f"标签：{row['name']}" if row else "标签"
        self.page_title.setText(titles.get(key, "全部内容"))
        active_key = f"{key}:{value}" if key in ("collection", "tag") else key
        self.sidebar.set_active(active_key)
        self.refresh_items()

    def open_day(self, day: str) -> None:
        self.current_kind = None
        self.current_favorite = False
        self.current_collection = None
        self.current_tag = None
        self.current_day = day
        self.current_recent = False
        self.page_title.setText(f"{day} 的内容")
        self.sidebar.set_active("date")
        self.refresh_items()

    def select_item(self, item_id: int) -> None:
        if item_id not in {item["id"] for item in self.current_items}:
            return
        self.current_item_id = item_id
        self.grid.selected_id = item_id
        self.table.selected_id = item_id
        if self.detail.isVisible():
            self.update_detail(item_id)

    def clear_item_selection(self) -> None:
        self.current_item_id = None
        self.grid.selected_id = None
        self.table.selected_id = None
        self.detail.clear_item()

    def update_detail(self, item_id: int) -> None:
        item = self.database.get_item(item_id)
        if item and self.current_item_id == item_id:
            if not self.detail.set_item(item):
                if self.current_item_id == item_id:
                    refreshed_item = self.database.get_item(item_id)
                    if refreshed_item is not None:
                        self.detail.set_item(refreshed_item)
                return
            if item_id in self._ai_requests:
                self.detail.set_ai_busy(True)
            if item_id in self._ocr_requests:
                self.detail.set_ocr_busy(True)

    def toggle_detail(self) -> None:
        if self.detail.isVisible():
            self.hide_detail()
        else:
            self.detail.setVisible(True)
            self.detail_button.setToolTip("收起详情")
            if self.current_item_id:
                self.update_detail(self.current_item_id)

    def hide_detail(self) -> None:
        self.detail.setVisible(False)
        self.detail_button.setToolTip("显示详情")

    def set_view_mode(self, mode: str) -> None:
        mode = "list" if mode == "list" else "grid"
        self.view_stack.setCurrentWidget(self.grid if mode == "grid" else self.table_page)
        self.grid.set_preview_loading_enabled(mode == "grid" and self.isVisible())
        self._refresh_visible_view()
        active_view = self.grid if mode == "grid" else self.table
        active_view.selected_id = self.current_item_id
        active_view.sync_selection_from_selected_id()
        self._save_setting("view_mode", mode)
        self.grid_button.setProperty("viewSelected", mode == "grid")
        self.list_button.setProperty("viewSelected", mode == "list")
        for button in (self.grid_button, self.list_button):
            button.style().unpolish(button)
            button.style().polish(button)

    def open_sort_menu(self) -> None:
        if self.sort_menu is not None and self.sort_menu.isVisible():
            self.sort_menu.close()
            return
        if time.monotonic() - self.sort_menu_closed_at < 0.2:
            return
        menu = QMenu(self)
        self.sort_menu = menu
        menu.aboutToHide.connect(self._sort_menu_hidden)
        entries = [("newest", "捕获时间：最新优先"), ("oldest", "捕获时间：最早优先"), ("name", "名称"), ("size", "文件大小"), ("type", "类型")]
        for key, label in entries:
            action = menu.addAction(("✓  " if key == self.current_sort else "    ") + label)
            action.triggered.connect(lambda _checked=False, value=key, text=label: self.set_sort(value, text))
        menu.popup(self.sort_button.mapToGlobal(self.sort_button.rect().bottomLeft()))

    def _sort_menu_hidden(self) -> None:
        self.sort_menu_closed_at = time.monotonic()
        menu = self.sort_menu
        self.sort_menu = None
        if menu is not None:
            menu.deleteLater()

    def set_sort(self, key: str, label: str) -> None:
        self.current_sort = key
        self._save_setting("sort", key)
        self.sort_button.setText(SORT_BUTTON_LABELS.get(key, label) + "  ▾")
        self.refresh_items()

    def toggle_monitor(self) -> None:
        self.clipboard_service.toggle()
        self._save_setting("monitoring", self.clipboard_service.timer.isActive())

    def _save_setting(self, key: str, value) -> None:
        try:
            self.settings.set(key, value)
        except OSError as exc:
            self.show_error_status(f"设置无法保存：{exc}")

    def update_monitor_button(self, active: bool) -> None:
        self.capture_status.set_active(active)
        if hasattr(self, "tray_monitor_action"):
            self.tray_monitor_action.setText("暂停监听" if active else "继续监听")
            self.tray.setToolTip("ClipSave - " + ("正在监听剪贴板" if active else "监听已暂停"))

    def on_captured(self, _item_id: int) -> None:
        self.show_status("已保存一条新的剪贴板内容")
        if self._semantic_request is not None or self._semantic_results_active:
            self._refresh_navigation_metadata()
        else:
            self.refresh_library()

    def activate_item(self, item_id: int) -> None:
        item = self.database.get_item(item_id)
        if not item:
            return
        if item["kind"] == "markdown":
            self._exec_transient_dialog(
                MarkdownDialog(item["title"], item["content"], item["path"], self)
            )
        elif item["kind"] == "image" and item["path"]:
            path = Path(item["path"])
            if not path.exists():
                QMessageBox.warning(self, "打开失败", "图片文件不存在或已被移动。")
                self.show_status("打开失败：图片文件不存在")
                return
            try:
                os.startfile(path)
            except OSError as exc:
                QMessageBox.warning(self, "打开失败", f"无法打开图片文件。\n\n{exc}")
                self.show_status("打开失败：图片文件无法访问")
        else:
            self.copy_item(item_id)

    def copy_item(self, item_id: int) -> None:
        item = self.database.get_item(item_id)
        if not item:
            return
        if self._copy_request is not None:
            self._cancel_async_token(self._copy_request[0])
            self._async_signals.discard(self._copy_request[1])
            self._copy_request = None
        clipboard = QApplication.clipboard()
        if item["kind"] == "image" and item["path"]:
            path = Path(item["path"])
            if not path.exists():
                QMessageBox.warning(self, "复制失败", "图片文件不存在或已被移动。")
                self.show_status("复制失败：图片文件不存在")
                return
            try:
                snapshot = preflight_image_file(path)
            except Exception as exc:
                QMessageBox.warning(self, "复制失败", str(exc))
                self.show_status("复制失败：图片文件无法读取")
                return
            token = object()
            signals = AsyncSignals()
            self._async_signals.add(signals)
            self._copy_request = (token, signals, item_id)
            signals.succeeded.connect(
                lambda result_item_id, _text, image, request_token=token, request_signals=signals: self._copy_image_succeeded(
                    request_token, request_signals, result_item_id, image
                )
            )
            signals.failed.connect(
                lambda message, request_token=token, request_signals=signals: self._copy_image_failed(
                    request_token, request_signals, message
                )
            )

            def work(cancel_event: threading.Event) -> None:
                try:
                    snapshot.require_current()
                    image = QImage(str(snapshot.path))
                    snapshot.require_current()
                    if image.isNull():
                        raise ValueError("图片文件无法读取，可能已损坏或无权访问。")
                    if not cancel_event.is_set():
                        signals.succeeded.emit(item_id, "", image)
                except Exception as exc:
                    if not cancel_event.is_set():
                        signals.failed.emit(str(exc))

            try:
                self._start_bounded_task(token, work, estimated_bytes=snapshot.decoded_bytes)
            except TaskCapacityExceeded as exc:
                self._copy_request = None
                self._async_signals.discard(signals)
                QMessageBox.warning(self, "复制任务繁忙", str(exc))
            return
        else:
            text = item["content"]
            self.clipboard_service.suppress_text(text)
            clipboard.setText(text)
        self.show_status("已复制到剪贴板")

    def _copy_image_succeeded(self, token: object, signals: AsyncSignals, item_id: int, image: QImage) -> None:
        self._async_signals.discard(signals)
        self._finish_async_token(token)
        if self._closing or self._copy_request != (token, signals, item_id):
            return
        self._copy_request = None
        self.clipboard_service.suppress_image(image)
        QApplication.clipboard().setImage(image)
        self.show_status("已复制到剪贴板")

    def _copy_image_failed(self, token: object, signals: AsyncSignals, message: str) -> None:
        self._async_signals.discard(signals)
        self._finish_async_token(token)
        if self._closing or self._copy_request is None or self._copy_request[:2] != (token, signals):
            return
        self._copy_request = None
        QMessageBox.warning(self, "复制失败", message)
        self.show_status("复制失败：图片文件无法读取")

    def delete_item(self, item_id: int) -> None:
        item = self.database.get_item(item_id)
        if not item:
            return
        message = f"要从 ClipSave 中删除“{item['title']}”吗？"
        managed_file = bool(item["path"] and is_under_local_store(Path(item["path"])))
        if managed_file:
            message += "\n\n对应文件将移入 Windows 回收站。"
        elif item["path"]:
            message += "\n\n该文件不在 ClipSave 本地资料库中，只会移除索引，原文件不会被删除。"
        answer = QMessageBox.question(
            self,
            "删除内容",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        managed_file = bool(item["path"] and is_under_local_store(Path(item["path"])))
        file_exists = bool(item["path"] and Path(item["path"]).exists())
        if managed_file and file_exists:
            try:
                if not is_under_local_store(Path(item["path"])):
                    raise RuntimeError("文件路径在确认期间发生变化，已取消删除。")
                recycle_managed_file(
                    Path(item["path"]),
                    LIBRARY_DIR,
                    send2trash,
                    expected_sha256=item["content_hash"],
                    expected_size=item["file_size"],
                )
            except Exception as exc:
                QMessageBox.warning(self, "删除失败", f"文件无法移入回收站，内容未删除。\n\n{exc}")
                self.show_status("删除失败：文件未能移入回收站")
                return
        try:
            self.database.remove_item(item_id)
        except Exception as exc:
            if managed_file and file_exists:
                try:
                    self.database.mark_item_missing(item_id)
                except Exception:
                    self._session_hidden_item_ids.add(item_id)
                self._cancel_item_requests(item_id)
                self._refresh_after_mutation()
                message = f"文件已移入回收站，但 ClipSave 索引删除失败。该条目已在当前会话隐藏，并会在下次启动时重新核对。\n\n{exc}"
                status = "文件已移入回收站，失效条目已隐藏"
            else:
                message = f"ClipSave 索引删除失败，内容未删除。\n\n{exc}"
                status = "删除失败：内容索引未能移除"
            QMessageBox.warning(self, "删除失败", message)
            self.show_status(status)
            return
        self._cancel_item_requests(item_id)
        if self.current_item_id == item_id:
            self.current_item_id = None
            self.detail.clear_item()
            self.hide_detail()
        self._refresh_after_mutation()
        if managed_file and file_exists:
            status = "内容及文件已移入回收站"
        elif managed_file:
            status = "内容索引已移除，文件已不存在"
        elif item["path"]:
            status = "内容索引已移除，原文件未删除"
        else:
            status = "内容已删除"
        self.show_status(status)

    def set_favorite(self, item_id: int, value: bool) -> None:
        if not self._run_database_action(
            lambda: self.database.set_favorite(item_id, value), "收藏更新失败", "收藏状态未保存"
        ):
            return
        self._refresh_after_mutation()
        if self.detail.isVisible() and self.current_item_id == item_id:
            self.update_detail(item_id)

    def save_notes(self, item_id: int, notes: str) -> bool:
        if not self._run_database_action(
            lambda: self.database.set_notes(item_id, notes), "备注保存失败", "备注未保存"
        ):
            return False
        self.detail.mark_notes_saved(item_id, notes)
        if self.search.text().strip() and not self._semantic_results_active:
            self.refresh_items()
        self.show_status("备注已保存")
        return True

    def add_collection(self) -> None:
        name, ok = QInputDialog.getText(self, "新建集合", "集合名称")
        if ok and name.strip():
            if not self._run_database_action(
                lambda: self.database.create_collection(name.strip()), "集合创建失败", "集合未创建"
            ):
                return
            self._refresh_after_mutation()

    def add_global_tag(self) -> None:
        if not self.current_item_id:
            QMessageBox.information(self, "添加标签", "请先选择一项内容。")
            return
        self.add_tag_to_item(self.current_item_id)

    def add_tag_to_item(self, item_id: int) -> None:
        name, ok = QInputDialog.getText(self, "添加标签", "标签名称")
        if ok and name.strip():
            if not self._run_database_action(
                lambda: self.database.add_tag(item_id, name.strip()), "标签添加失败", "标签未添加"
            ):
                return
            self._refresh_after_mutation()
            self.update_detail(item_id)

    def remove_tag_from_item(self, item_id: int, name: str) -> None:
        if not self._run_database_action(
            lambda: self.database.remove_tag(item_id, name), "标签移除失败", "标签未移除"
        ):
            return
        self._refresh_after_mutation()
        if self.current_item_id == item_id:
            self.update_detail(item_id)

    def set_item_collection(self, item_id: int, collection_id) -> None:
        if not self._run_database_action(
            lambda: self.database.set_collection(item_id, collection_id), "集合更新失败", "集合未更新"
        ):
            if self.current_item_id == item_id:
                self.update_detail(item_id)
            return
        self._refresh_after_mutation()
        self.show_status("集合已更新")

    def _refresh_after_mutation(self) -> None:
        if not self._semantic_results_active:
            self.refresh_library()
            return
        self._refresh_navigation_metadata()
        base_items = self.database.query_items(
            kind=self.current_kind,
            favorite=self.current_favorite,
            day=self.current_day,
            recent_days=7 if self.current_recent else None,
            collection_id=self.current_collection,
            tag_id=self.current_tag,
            sort=self.current_sort,
            summary_only=True,
        )
        records = {row["id"]: row for row in base_items}
        self._apply_items([records[item_id] for item_id in self._semantic_ordered_ids if item_id in records])
        self.result_count.setText(f"{len(self.current_items):,} 项 · 按语义相关度")

    def _run_database_action(self, action, title: str, status: str) -> bool:
        try:
            action()
            return True
        except Exception as exc:
            QMessageBox.warning(self, title, str(exc))
            self.show_status(status)
            return False

    def import_files(self, parent=None) -> None:
        if self._import_request is not None:
            QMessageBox.information(self, "正在导入", "上一批文件仍在导入，请稍候。")
            return
        paths, _ = QFileDialog.getOpenFileNames(parent or self, "导入内容", "", "支持的文件 (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.md)")
        if not paths:
            return
        token = object()
        signals = AsyncSignals()
        self._async_signals.add(signals)
        self._import_request = (token, signals)
        signals.succeeded.connect(
            lambda _item_id, _text, result, request_token=token, request_signals=signals: self._import_finished(
                request_token, request_signals, result
            )
        )
        signals.failed.connect(
            lambda message, request_token=token, request_signals=signals: self._import_failed(
                request_token, request_signals, message
            )
        )
        self.show_status(f"正在导入 {len(paths)} 个文件")

        def work(cancel_event: threading.Event) -> None:
            added = 0
            localized = 0
            duplicates = 0
            processed = 0
            failed = []
            for filename in paths:
                if cancel_event.is_set():
                    break
                try:
                    candidate = Path(filename)
                    if candidate.suffix.lower() == ".md":
                        if candidate.stat().st_size > MAX_MARKDOWN_BYTES:
                            raise ValueError("Markdown 文件过大，已拒绝导入。")
                        candidate.read_text(encoding="utf-8", errors="replace")
                    else:
                        preflight_image_file(candidate, max_file_bytes=MAX_IMPORT_BYTES)
                    import_result = self.database.import_file(
                        candidate, copy_to_library=True, strict=True
                    )
                    if import_result is ImportFileResult.LOCALIZED:
                        localized += 1
                    elif import_result:
                        added += int(import_result)
                    else:
                        duplicates += 1
                except Exception as exc:
                    failed.append((Path(filename).name, str(exc)))
                finally:
                    processed += 1
            signals.succeeded.emit(
                -1,
                "",
                {
                    "total": len(paths),
                    "added": added,
                    "localized": localized,
                    "duplicates": duplicates,
                    "processed": processed,
                    "failed": failed,
                    "cancelled": cancel_event.is_set(),
                },
            )

        self._start_async_task(token, work)

    def _import_finished(self, token: object, signals: AsyncSignals, result: dict) -> None:
        self._async_signals.discard(signals)
        if self._closing or self._quit_in_progress or self._import_request != (token, signals):
            return
        self._import_request = None
        self.refresh_library()
        failed = result["failed"]
        localized = result.get("localized", 0)
        skipped = result.get(
            "duplicates",
            result["total"] - result["added"] - localized - len(failed),
        )
        unprocessed = max(0, result["total"] - result.get("processed", result["total"]))
        prefix = "导入已取消；" if result.get("cancelled") else ""
        unprocessed_text = f"，未处理 {unprocessed} 项" if unprocessed else ""
        if localized:
            self.show_status(
                f"{prefix}已导入 {result['added']} 项，本地化 {localized} 项，"
                f"跳过 {skipped} 项重复内容，失败 {len(failed)} 项{unprocessed_text}"
            )
        else:
            self.show_status(
                f"{prefix}已导入 {result['added']} 项，跳过 {skipped} 项重复内容，"
                f"失败 {len(failed)} 项{unprocessed_text}"
            )
        if failed:
            details = "\n".join(f"{name}：{message}" for name, message in failed[:5])
            if len(failed) > 5:
                details += f"\n另有 {len(failed) - 5} 项失败。"
            QMessageBox.warning(self, "部分文件导入失败", f"以下文件无法导入，其他文件已继续处理：\n\n{details}")

    def _import_failed(self, token: object, signals: AsyncSignals, message: str) -> None:
        self._async_signals.discard(signals)
        if self._closing or self._quit_in_progress or self._import_request != (token, signals):
            return
        self._import_request = None
        QMessageBox.warning(self, "文件导入失败", message)

    def open_settings(self) -> None:
        previous_follow_system = self.settings.get("follow_system_theme", True)
        previous_theme_mode = self.settings.get("theme_mode", "light")
        dialog = SettingsDialog(self.settings, self)
        dialog.import_requested.connect(lambda: self.import_files(dialog))
        result = self._exec_transient_dialog(dialog)
        if result and (
            self.settings.get("follow_system_theme", True) != previous_follow_system
            or self.settings.get("theme_mode", "light") != previous_theme_mode
        ):
            self.apply_theme(force=True)

    def _start_async_task(self, token: object, target) -> threading.Event:
        cancel_event = threading.Event()

        def run() -> None:
            try:
                target(cancel_event)
            finally:
                with self._async_tasks_lock:
                    current = self._async_tasks.get(token)
                    if current is not None and current[1] is threading.current_thread():
                        self._async_tasks.pop(token, None)

        thread = threading.Thread(target=run, name="ClipSaveAsync", daemon=True)
        with self._async_tasks_lock:
            self._async_tasks[token] = (cancel_event, thread)
        thread.start()
        return cancel_event

    def _start_bounded_task(self, token: object, target, *, estimated_bytes: int = 0):
        with self._async_tasks_lock:
            self._bounded_tasks = {
                key: handle for key, handle in self._bounded_tasks.items() if not handle.done_event.is_set()
            }
        handle = ai_ocr_task_executor().submit(target, estimated_bytes=estimated_bytes)
        with self._async_tasks_lock:
            self._bounded_tasks[token] = handle
        return handle

    def _start_startup_scan(self, full_scan: bool, reconcile_images: bool) -> None:
        self.startup_scan_error = None
        token = object()
        signals = AsyncSignals()
        self._async_signals.add(signals)
        self._startup_scan_request = (token, signals)
        signals.succeeded.connect(
            lambda _item_id, _text, imported, request_token=token, request_signals=signals: self._startup_scan_finished(
                request_token, request_signals, int(imported)
            )
        )
        signals.failed.connect(
            lambda message, request_token=token, request_signals=signals: self._startup_scan_failed(
                request_token, request_signals, message
            )
        )

        def work(cancel_event: threading.Event) -> None:
            try:
                self.database.mark_missing_files(cancel_event)
                imported = 0
                if not cancel_event.is_set() and full_scan:
                    imported = self.database.scan_legacy_files(cancel_event)
                elif not cancel_event.is_set() and reconcile_images:
                    imported = self.database.scan_unindexed_files(cancel_event)
                signals.succeeded.emit(-1, "", imported)
            except Exception as exc:
                if not cancel_event.is_set():
                    signals.failed.emit(str(exc))

        self._start_async_task(token, work)

    def _start_periodic_backup(self) -> None:
        if self._closing or self._quit_in_progress or self._backup_request is not None:
            return
        if not self.database.backup_state()["dirty"]:
            return
        token = object()
        signals = AsyncSignals()
        self._async_signals.add(signals)
        self._backup_request = (token, signals)
        signals.succeeded.connect(
            lambda _item_id, _text, path, request_token=token, request_signals=signals: self._periodic_backup_finished(
                request_token, request_signals, path
            )
        )
        signals.failed.connect(
            lambda message, request_token=token, request_signals=signals: self._periodic_backup_failed(
                request_token, request_signals, message
            )
        )

        def work(_cancel_event: threading.Event) -> None:
            try:
                path = self.database.create_backup_if_changed()
                signals.succeeded.emit(-1, "", str(path) if path else "")
            except Exception as exc:
                signals.failed.emit(str(exc))

        self._start_async_task(token, work)

    def _periodic_backup_finished(self, token: object, signals: AsyncSignals, _path: str) -> None:
        self._async_signals.discard(signals)
        if self._backup_request == (token, signals):
            self._backup_request = None

    def _periodic_backup_failed(self, token: object, signals: AsyncSignals, message: str) -> None:
        self._async_signals.discard(signals)
        if self._closing or self._backup_request != (token, signals):
            return
        self._backup_request = None
        self.show_error_status(f"数据库备份失败：{message}")

    def _show_database_recovery_state(self) -> None:
        state = self.database.recovery_state()
        backup = self.database.backup_state()
        if state["action"] == "none" and not backup["last_error"]:
            return
        details = []
        if state["action"] == "restored":
            details.append(f"数据库已从备份恢复：{state['backup_path']}")
        elif state["action"] == "rebuilt":
            details.append("数据库无法恢复，已重建索引。原数据库文件已保留。")
        if state["preserved_paths"]:
            details.append("保留文件：\n" + "\n".join(state["preserved_paths"]))
        if backup["last_error"]:
            details.append(f"最近一次备份失败：{backup['last_error']}")
        QMessageBox.warning(self, "ClipSave 数据库恢复", "\n\n".join(details))

    def _startup_scan_finished(self, token: object, signals: AsyncSignals, imported: int) -> None:
        self._async_signals.discard(signals)
        if self._closing or self._startup_scan_request != (token, signals):
            return
        self._startup_scan_request = None
        self.startup_scan_error = None
        self.refresh_library()
        if imported:
            self.show_status(f"已导入 {imported} 个现有文件")

    def _startup_scan_failed(self, token: object, signals: AsyncSignals, message: str) -> None:
        self._async_signals.discard(signals)
        if self._closing or self._startup_scan_request != (token, signals):
            return
        self._startup_scan_request = None
        self.startup_scan_error = message
        self.show_error_status(f"启动扫描失败：{message}")

    def _cancel_async_token(self, token: object) -> None:
        with self._async_tasks_lock:
            task = self._async_tasks.get(token)
            bounded = self._bounded_tasks.get(token)
        if task is not None:
            task[0].set()
        if bounded is not None:
            bounded.cancel()

    def _finish_async_token(self, token: object) -> None:
        with self._async_tasks_lock:
            self._bounded_tasks.pop(token, None)

    def _schedule_cancelled_request_cleanup(self, cancelled_tokens: set[object]) -> None:
        if not cancelled_tokens:
            return

        def token_done(token: object) -> bool:
            with self._async_tasks_lock:
                regular = self._async_tasks.get(token)
                bounded = self._bounded_tasks.get(token)
            return (
                (regular is None or not regular[1].is_alive())
                and (bounded is None or bounded.done_event.is_set())
            )

        def poll() -> None:
            if self._closing:
                return
            pending = False
            for requests, kind in (
                (self._ai_requests, "ai"),
                (self._ocr_requests, "ocr"),
            ):
                for item_id, request in list(requests.items()):
                    token, signals = request
                    if token not in cancelled_tokens:
                        continue
                    if not token_done(token):
                        pending = True
                        continue
                    requests.pop(item_id, None)
                    self._async_signals.discard(signals)
                    self._finish_async_token(token)
                    if self.current_item_id == item_id:
                        if kind == "ai":
                            self.detail.set_ai_busy(False)
                        else:
                            self.detail.set_ocr_busy(False)
            if self._semantic_request is not None:
                token, signals = self._semantic_request
                if token in cancelled_tokens:
                    if token_done(token):
                        self._semantic_request = None
                        self._async_signals.discard(signals)
                        self._finish_async_token(token)
                        self.semantic_button.setEnabled(True)
                        self.semantic_button.setText("语义搜索")
                    else:
                        pending = True
            if self._copy_request is not None:
                token, signals, _item_id = self._copy_request
                if token in cancelled_tokens:
                    if token_done(token):
                        self._copy_request = None
                        self._async_signals.discard(signals)
                    else:
                        pending = True
            if pending:
                QTimer.singleShot(100, poll)

        QTimer.singleShot(0, poll)

    def _cancel_request(self, request: tuple[object, AsyncSignals] | None) -> None:
        if request is None:
            return
        token, signals = request
        self._cancel_async_token(token)
        self._async_signals.discard(signals)

    def _cancel_item_requests(self, item_id: int) -> None:
        self._cancel_request(self._ai_requests.pop(item_id, None))
        self._cancel_request(self._ocr_requests.pop(item_id, None))

    def _cancel_and_wait_request(
        self,
        request: tuple[object, AsyncSignals] | None,
        timeout: float,
        *,
        process_events: bool = True,
    ) -> bool:
        if request is None:
            return True
        token, _signals = request
        self._cancel_async_token(token)
        with self._async_tasks_lock:
            task = self._async_tasks.get(token)
        if task is None:
            return True
        deadline = time.monotonic() + max(timeout, 0.0)
        app = QApplication.instance()
        while task[1].is_alive() and time.monotonic() < deadline:
            if process_events and app is not None:
                app.processEvents()
            wait_time = min(0.01, max(0.0, deadline - time.monotonic()))
            if wait_time <= 0:
                break
            task[1].join(wait_time)
        return not task[1].is_alive()

    def _cancel_and_wait_for_async_tasks(
        self,
        timeout: float = 1.5,
        *,
        require_bounded: bool = True,
        process_events: bool = True,
    ) -> bool:
        deadline = time.monotonic() + max(timeout, 0.0)
        app = QApplication.instance()
        while time.monotonic() < deadline:
            with self._async_tasks_lock:
                tasks = list(self._async_tasks.values())
                bounded_tasks = list(self._bounded_tasks.values())
            for cancel_event, _thread in tasks:
                cancel_event.set()
            for handle in bounded_tasks:
                handle.cancel()
            regular_done = all(not thread.is_alive() for _cancel_event, thread in tasks)
            bounded_done = all(handle.done_event.is_set() for handle in bounded_tasks)
            if regular_done and (bounded_done or not require_bounded):
                return True
            if process_events and app is not None:
                app.processEvents()
            for _cancel_event, thread in tasks:
                if thread.is_alive():
                    wait_time = min(0.005, max(0.0, deadline - time.monotonic()))
                    if wait_time <= 0:
                        break
                    thread.join(wait_time)
            for handle in bounded_tasks:
                if not handle.done_event.is_set():
                    wait_time = min(0.005, max(0.0, deadline - time.monotonic()))
                    if wait_time <= 0:
                        break
                    handle.wait(wait_time)
        with self._async_tasks_lock:
            tasks = list(self._async_tasks.values())
            bounded_tasks = list(self._bounded_tasks.values())
        regular_done = all(not thread.is_alive() for _cancel_event, thread in tasks)
        bounded_done = all(handle.done_event.is_set() for handle in bounded_tasks)
        return regular_done and (bounded_done or not require_bounded)

    def generate_ai_description(self, item_id: int) -> None:
        item = self.database.get_item(item_id)
        if not item or item["kind"] != "image" or not item["path"]:
            QMessageBox.information(self, "AI 描述", "当前只支持为图片生成 AI 描述。")
            return
        service = AIService(
            self.settings.get("ai_base_url", ""),
            self.settings.get("ai_api_key", ""),
            self.settings.get("ai_vision_model", ""),
            self.settings.get("ai_embedding_model", ""),
        )
        if not service.configured:
            QMessageBox.information(self, "AI 服务未配置", "请先在设置中填写 OpenAI-compatible 服务地址、API Key 和视觉模型。")
            self.open_settings()
            return
        try:
            image_snapshot = preflight_image_file(Path(item["path"]))
        except Exception as exc:
            QMessageBox.warning(self, "AI 描述失败", str(exc))
            return
        self.detail.ai_button.setEnabled(False)
        self.detail.ai_button.setText("生成中…")
        token = object()
        signals = AsyncSignals()
        self._cancel_request(self._ai_requests.get(item_id))
        self._async_signals.add(signals)
        self._ai_requests[item_id] = (token, signals)
        signals.succeeded.connect(
            lambda result_item_id, description, embedding, request_token=token, request_signals=signals: self._ai_succeeded(
                request_token, request_signals, result_item_id, description, embedding
            )
        )
        signals.failed.connect(
            lambda message, request_token=token, request_signals=signals, request_item_id=item_id: self._ai_failed(
                request_token, request_signals, request_item_id, message
            )
        )

        def work(cancel_event: threading.Event) -> None:
            try:
                description = service.describe_image(image_snapshot, cancel_event)
                embedding = service.embed(description, cancel_event)
                if not cancel_event.is_set():
                    signals.succeeded.emit(item_id, description, embedding)
            except OperationCancelled:
                return
            except Exception as exc:
                if not cancel_event.is_set():
                    signals.failed.emit(str(exc))

        try:
            self._start_bounded_task(token, work, estimated_bytes=image_snapshot.decoded_bytes)
        except TaskCapacityExceeded as exc:
            self._ai_requests.pop(item_id, None)
            self._async_signals.discard(signals)
            if self.current_item_id == item_id:
                self.detail.set_ai_busy(False, failed=True)
            QMessageBox.warning(self, "AI 任务繁忙", str(exc))

    def generate_ocr(self, item_id: int) -> None:
        item = self.database.get_item(item_id)
        if not item or item["kind"] != "image" or not item["path"]:
            QMessageBox.information(self, "OCR", "当前只支持识别图片中的文字。")
            return
        try:
            image_snapshot = preflight_image_file(Path(item["path"]))
        except Exception as exc:
            QMessageBox.warning(self, "OCR 识别失败", str(exc))
            return
        self.detail.ocr_button.setEnabled(False)
        self.detail.ocr_button.setText("识别中…")
        token = object()
        signals = AsyncSignals()
        self._cancel_request(self._ocr_requests.get(item_id))
        self._async_signals.add(signals)
        self._ocr_requests[item_id] = (token, signals)
        signals.succeeded.connect(
            lambda result_item_id, text, unused, request_token=token, request_signals=signals: self._ocr_succeeded(
                request_token, request_signals, result_item_id, text, unused
            )
        )
        signals.failed.connect(
            lambda message, request_token=token, request_signals=signals, request_item_id=item_id: self._ocr_failed(
                request_token, request_signals, request_item_id, message
            )
        )

        def work(cancel_event: threading.Event) -> None:
            try:
                text = WindowsOCRService.recognize(Path(item["path"]), cancel_event)
                if not cancel_event.is_set():
                    signals.succeeded.emit(item_id, text, None)
            except OperationCancelled:
                return
            except Exception as exc:
                if not cancel_event.is_set():
                    signals.failed.emit(str(exc))

        try:
            self._start_bounded_task(token, work, estimated_bytes=image_snapshot.decoded_bytes)
        except TaskCapacityExceeded as exc:
            self._ocr_requests.pop(item_id, None)
            self._async_signals.discard(signals)
            if self.current_item_id == item_id:
                self.detail.set_ocr_busy(False, failed=True)
            QMessageBox.warning(self, "OCR 任务繁忙", str(exc))

    def _ocr_succeeded(self, token: object, signals: AsyncSignals, item_id: int, text: str, _unused) -> None:
        self._async_signals.discard(signals)
        self._finish_async_token(token)
        if self._closing or self._quit_in_progress:
            return
        if self._ocr_requests.get(item_id) != (token, signals):
            return
        if self.database.get_item(item_id) is None:
            self._ocr_requests.pop(item_id, None)
            return
        try:
            self.database.update_ocr(item_id, text)
        except Exception as exc:
            self._ocr_failed(token, signals, item_id, f"OCR 结果无法保存：{exc}")
            return
        self._ocr_requests.pop(item_id, None)
        self._refresh_after_mutation()
        if self.current_item_id == item_id:
            self.update_detail(item_id)
        self.show_status("OCR 识别完成" if text else "图片中未识别到文字")

    def _ocr_failed(self, token: object, signals: AsyncSignals, item_id: int, message: str) -> None:
        self._async_signals.discard(signals)
        self._finish_async_token(token)
        if self._closing or self._quit_in_progress:
            return
        if self._ocr_requests.get(item_id) != (token, signals):
            return
        self._ocr_requests.pop(item_id, None)
        if self.current_item_id == item_id:
            self.detail.set_ocr_busy(False, failed=True)
        QMessageBox.warning(self, "OCR 识别失败", message)

    def semantic_search(self) -> None:
        query = self.search.text().strip()
        if not query:
            QMessageBox.information(self, "语义搜索", "先输入要查找的画面或含义。")
            return
        service = AIService(
            self.settings.get("ai_base_url", ""),
            self.settings.get("ai_api_key", ""),
            self.settings.get("ai_vision_model", ""),
            self.settings.get("ai_embedding_model", ""),
        )
        if not service.configured or not service.embedding_model:
            QMessageBox.information(self, "语义搜索未配置", "请在设置中填写兼容服务、视觉模型和向量模型。")
            self.open_settings()
            return
        embedded = self.database.embedded_items()
        if not embedded:
            QMessageBox.information(self, "没有语义索引", "请先在图片详情中生成 AI 描述和向量。")
            return
        self.search_timer.stop()
        self._cancel_semantic_request()
        self.semantic_button.setEnabled(False)
        self.semantic_button.setText("搜索中…")
        token = object()
        signals = AsyncSignals()
        self._async_signals.add(signals)
        self._semantic_request = (token, signals)
        signals.succeeded.connect(
            lambda item_id, text, ordered_ids, request_token=token, request_signals=signals, request_query=query: self._semantic_succeeded(
                request_token, request_signals, request_query, item_id, text, ordered_ids
            )
        )
        signals.failed.connect(
            lambda message, request_token=token, request_signals=signals: self._semantic_failed(
                request_token, request_signals, message
            )
        )

        def work(cancel_event: threading.Event) -> None:
            try:
                query_vector = service.embed(query, cancel_event)
                scored = []
                for row in embedded:
                    if cancel_event.is_set():
                        raise OperationCancelled("Operation cancelled")
                    vector = json.loads(row["embedding"])
                    scored.append((service.similarity(query_vector, vector), row["id"]))
                scored.sort(reverse=True)
                if not cancel_event.is_set():
                    signals.succeeded.emit(-1, "", [item_id for score, item_id in scored if score > 0])
            except OperationCancelled:
                return
            except Exception as exc:
                if not cancel_event.is_set():
                    signals.failed.emit(str(exc))

        try:
            self._start_bounded_task(token, work, estimated_bytes=min(len(embedded) * 4096, 64 * 1024 * 1024))
        except TaskCapacityExceeded as exc:
            self._semantic_request = None
            self._async_signals.discard(signals)
            self.semantic_button.setEnabled(True)
            self.semantic_button.setText("语义搜索")
            QMessageBox.warning(self, "语义搜索繁忙", str(exc))

    def _semantic_succeeded(self, token: object, signals: AsyncSignals, query: str, _item_id: int, _text: str, ordered_ids) -> None:
        self._async_signals.discard(signals)
        self._finish_async_token(token)
        if self._closing:
            return
        if self._semantic_request != (token, signals):
            return
        self._semantic_request = None
        self.semantic_button.setEnabled(True)
        self.semantic_button.setText("语义搜索")
        if self.search.text().strip() != query:
            return
        base_items = self.database.query_items(
            kind=self.current_kind,
            favorite=self.current_favorite,
            day=self.current_day,
            recent_days=7 if self.current_recent else None,
            collection_id=self.current_collection,
            tag_id=self.current_tag,
            sort=self.current_sort,
            summary_only=True,
        )
        records = {row["id"]: row for row in base_items}
        self._apply_items([records[item_id] for item_id in ordered_ids if item_id in records])
        self._semantic_ordered_ids = list(ordered_ids)
        self._semantic_results_active = True
        self.result_count.setText(f"{len(self.current_items):,} 项 · 按语义相关度")

    def _semantic_failed(self, token: object, signals: AsyncSignals, message: str) -> None:
        self._async_signals.discard(signals)
        self._finish_async_token(token)
        if self._closing:
            return
        if self._semantic_request != (token, signals):
            return
        self._semantic_request = None
        self.semantic_button.setEnabled(True)
        self.semantic_button.setText("语义搜索")
        QMessageBox.warning(self, "语义搜索失败", message)

    def _cancel_semantic_request(self) -> None:
        if self._semantic_request is None:
            return
        self._cancel_request(self._semantic_request)
        self._semantic_request = None
        if hasattr(self, "semantic_button"):
            self.semantic_button.setEnabled(True)
            self.semantic_button.setText("语义搜索")

    def _ai_succeeded(self, token: object, signals: AsyncSignals, item_id: int, description: str, embedding) -> None:
        self._async_signals.discard(signals)
        self._finish_async_token(token)
        if self._closing or self._quit_in_progress:
            return
        if self._ai_requests.get(item_id) != (token, signals):
            return
        if self.database.get_item(item_id) is None:
            self._ai_requests.pop(item_id, None)
            return
        try:
            self.database.update_ai(item_id, description, embedding)
        except Exception as exc:
            self._ai_failed(token, signals, item_id, f"AI 结果无法保存：{exc}")
            return
        self._ai_requests.pop(item_id, None)
        self._refresh_after_mutation()
        if self.current_item_id == item_id:
            self.update_detail(item_id)
        self.show_status("AI 描述已生成")

    def _ai_failed(self, token: object, signals: AsyncSignals, item_id: int, message: str) -> None:
        self._async_signals.discard(signals)
        self._finish_async_token(token)
        if self._closing or self._quit_in_progress:
            return
        if self._ai_requests.get(item_id) != (token, signals):
            return
        self._ai_requests.pop(item_id, None)
        if self.current_item_id == item_id:
            self.detail.set_ai_busy(False, failed=True)
        QMessageBox.warning(self, "AI 服务失败", message)

    def focus_search(self) -> None:
        self.bring_to_front()
        self.search.setFocus()
        self.search.selectAll()

    def bring_to_front(self) -> None:
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized | Qt.WindowState.WindowActive)
        self.raise_()
        self.activateWindow()

    def showEvent(self, event) -> None:
        self._ensure_native_resize_frame()
        super().showEvent(event)
        self.grid.set_preview_loading_enabled(self.view_stack.currentWidget() is self.grid)
        if not self._initial_position_constrained:
            self._initial_position_constrained = True
            QTimer.singleShot(0, self._constrain_to_available_screen)

    def _constrain_to_available_screen(self) -> None:
        if self.isMaximized() or self.isFullScreen():
            return
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        width = min(self.width(), available.width())
        height = min(self.height(), available.height())
        x = min(max(self.x(), available.left()), available.right() - width + 1)
        y = min(max(self.y(), available.top()), available.bottom() - height + 1)
        if (
            not available.contains(self.geometry())
            or width != self.width()
            or height != self.height()
        ):
            self.setGeometry(x, y, width, height)

    def hideEvent(self, event) -> None:
        self.grid.set_preview_loading_enabled(False)
        super().hideEvent(event)

    def show_status(self, text: str) -> None:
        self._status_generation = getattr(self, "_status_generation", 0) + 1
        generation = self._status_generation
        self.capture_status.setToolTip(text)
        QTimer.singleShot(
            2800,
            lambda: self._restore_capture_tooltip(generation),
        )

    def show_error_status(self, text: str) -> None:
        self._status_generation = getattr(self, "_status_generation", 0) + 1
        generation = self._status_generation
        message = f"ClipSave 操作失败：{text[:160]}"
        self.capture_status.setToolTip(message)
        QTimer.singleShot(5000, lambda: self._restore_capture_tooltip(generation))
        if hasattr(self, "tray") and self.tray.isVisible():
            self.tray.showMessage("ClipSave", message, QSystemTrayIcon.MessageIcon.Warning, 5000)

    def _restore_capture_tooltip(self, generation: int) -> None:
        if generation != getattr(self, "_status_generation", 0):
            return
        self.capture_status.setToolTip(
            "本地自动捕获已开启" if self.clipboard_service.timer.isActive() else "本地自动捕获已暂停"
        )

    def quit_application_for_session_end(self, timeout: float) -> bool:
        if self._closing:
            return True
        if self._quit_in_progress or timeout <= 0:
            return False
        deadline = time.monotonic() + timeout
        cancelled_request_tokens: set[object] = set()

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        def abort(monitoring_was_active: bool) -> bool:
            self.clipboard_service.resume_after_failed_shutdown(monitoring_was_active)
            self.backup_timer.start()
            self._quit_in_progress = False
            self.force_quit = False
            self._set_interactions_enabled(True)
            current_id = self.current_item_id
            self.detail.set_ai_busy(bool(current_id and current_id in self._ai_requests))
            self.detail.set_ocr_busy(bool(current_id and current_id in self._ocr_requests))
            if self._semantic_request is None:
                self.semantic_button.setEnabled(True)
                self.semantic_button.setText("语义搜索")
            self._schedule_cancelled_request_cleanup(cancelled_request_tokens)
            return False

        self._quit_in_progress = True
        self.force_quit = True
        self._set_interactions_enabled(False)
        self.search_timer.stop()
        monitoring_was_active = self.clipboard_service.timer.isActive()
        self.clipboard_service.prepare_for_shutdown()

        note_updates = self.detail.pending_note_updates()
        if self.detail.current_item is not None:
            item_id = self.detail.current_item["id"]
            notes = self.detail.notes.toPlainText()
            if notes != self.detail._loaded_notes:
                note_updates[item_id] = (self.detail._loaded_notes, notes)
        if note_updates:
            note_result: list[Exception | None] = []
            note_saved_ids: list[int] = []
            note_done = threading.Event()

            def persist_notes() -> None:
                error = None
                try:
                    for item_id, (expected_notes, notes) in note_updates.items():
                        if not self.database.set_notes_if_unchanged(
                            item_id, expected_notes, notes
                        ):
                            raise RuntimeError("notes changed during session shutdown")
                        note_saved_ids.append(item_id)
                except Exception as exc:
                    error = exc
                finally:
                    note_result.append(error)
                    note_done.set()

            threading.Thread(
                target=persist_notes,
                name="ClipSaveSessionNotes",
                daemon=True,
            ).start()
            notes_finished = note_done.wait(remaining())
            for item_id in list(note_saved_ids):
                _expected_notes, notes = note_updates[item_id]
                self.detail.mark_notes_saved(item_id, notes)
            if not notes_finished or note_result != [None]:
                return abort(monitoring_was_active)

        for attribute in ("_startup_scan_request", "_import_request", "_backup_request"):
            request = getattr(self, attribute)
            self._cancel_request(request)
            if not self._cancel_and_wait_request(
                request, remaining(), process_events=False
            ):
                return abort(monitoring_was_active)
            setattr(self, attribute, None)
        self.backup_timer.stop()
        semantic_request = self._semantic_request
        self._cancel_request(semantic_request)
        if semantic_request is not None:
            cancelled_request_tokens.add(semantic_request[0])
        for request in list(self._ai_requests.values()):
            self._cancel_request(request)
            cancelled_request_tokens.add(request[0])
        for request in list(self._ocr_requests.values()):
            self._cancel_request(request)
            cancelled_request_tokens.add(request[0])
        if self._copy_request is not None:
            self._cancel_async_token(self._copy_request[0])
            self._async_signals.discard(self._copy_request[1])
            cancelled_request_tokens.add(self._copy_request[0])
        if not self._cancel_and_wait_for_async_tasks(
            remaining(), require_bounded=True, process_events=False
        ):
            return abort(monitoring_was_active)
        self._semantic_request = None
        self.semantic_button.setEnabled(True)
        self.semantic_button.setText("语义搜索")
        self._ai_requests.clear()
        self._ocr_requests.clear()
        self._copy_request = None

        if not self.clipboard_service.wait_for_idle(remaining()):
            return abort(monitoring_was_active)
        if remaining() <= 0:
            return abort(monitoring_was_active)
        try:
            self.database.create_backup()
        except Exception as exc:
            self.database.recovery_report["backup_error"] = str(exc)
            return abort(monitoring_was_active)
        if not self.clipboard_service.shutdown(timeout=remaining()):
            return abort(monitoring_was_active)

        self._closing = True
        self._async_signals.clear()
        shutdown_ai_ocr_task_executor(timeout=min(remaining(), 0.1))
        self.grid.shutdown_thumbnail_loader(timeout_ms=0)
        self.detail.shutdown_thumbnail_loader(timeout_ms=0)
        self.database.close()
        self.tray.hide()
        application = QApplication.instance()
        if application is not None:
            application.exit(0)
        return True

    def quit_application(self) -> bool:
        if self._closing or self._quit_in_progress:
            return False
        self._quit_in_progress = True
        self.force_quit = True
        self._set_interactions_enabled(False)
        self.search_timer.stop()
        if not self.detail.flush_notes():
            self._set_interactions_enabled(True)
            self._quit_in_progress = False
            self.force_quit = False
            QMessageBox.warning(self, "备注保存失败", "当前备注尚未保存，ClipSave 已取消退出。")
            return False
        for item_id, notes in self.detail.pending_note_drafts().items():
            if self.save_notes(item_id, notes):
                continue
            self._set_interactions_enabled(True)
            self._quit_in_progress = False
            self.force_quit = False
            QMessageBox.warning(
                self,
                "备注保存失败",
                "仍有备注尚未保存，ClipSave 已取消退出。",
            )
            return False
        if not self._cancel_and_wait_request(self._startup_scan_request, 10.0):
            self._quit_in_progress = False
            self.force_quit = False
            self._set_interactions_enabled(True)
            QMessageBox.warning(
                self,
                "ClipSave 正在整理本地库",
                "本地文件扫描仍在结束。为避免数据库操作中断，ClipSave 暂时不会退出。请稍后再次退出。",
            )
            return False
        self._cancel_request(self._startup_scan_request)
        self._startup_scan_request = None
        if not self._cancel_and_wait_request(self._import_request, 10.0):
            self._quit_in_progress = False
            self.force_quit = False
            self._set_interactions_enabled(True)
            QMessageBox.warning(
                self,
                "ClipSave 正在导入",
                "当前文件仍在完成本地复制或校验。为避免产生不完整文件，ClipSave 暂时不会退出。请稍后再次退出。",
            )
            return False
        self._cancel_request(self._import_request)
        self._import_request = None
        self.backup_timer.stop()
        if not self._cancel_and_wait_request(self._backup_request, 10.0):
            self._quit_in_progress = False
            self.force_quit = False
            self._set_interactions_enabled(True)
            self.backup_timer.start()
            QMessageBox.warning(
                self,
                "ClipSave 正在备份",
                "数据库备份仍在完成。为避免备份损坏，ClipSave 暂时不会退出。请稍后再次退出。",
            )
            return False
        self._cancel_request(self._backup_request)
        self._backup_request = None
        cancelled_request_tokens: set[object] = set()
        semantic_request = self._semantic_request
        self._cancel_request(semantic_request)
        if semantic_request is not None:
            cancelled_request_tokens.add(semantic_request[0])
        for request in list(self._ai_requests.values()):
            self._cancel_request(request)
            cancelled_request_tokens.add(request[0])
        for request in list(self._ocr_requests.values()):
            self._cancel_request(request)
            cancelled_request_tokens.add(request[0])
        if self._copy_request is not None:
            self._cancel_async_token(self._copy_request[0])
            self._async_signals.discard(self._copy_request[1])
            cancelled_request_tokens.add(self._copy_request[0])
        if not self._cancel_and_wait_for_async_tasks(6.0):
            self._quit_in_progress = False
            self.force_quit = False
            self._set_interactions_enabled(True)
            self.backup_timer.start()
            self._schedule_cancelled_request_cleanup(cancelled_request_tokens)
            if self.current_item_id:
                self.update_detail(self.current_item_id)
            QMessageBox.warning(
                self,
                "ClipSave 正在结束后台任务",
                "AI、OCR 或图片处理任务仍在结束。ClipSave 暂时不会退出，请稍后再次退出。",
            )
            return False
        self._semantic_request = None
        self.semantic_button.setEnabled(True)
        self.semantic_button.setText("语义搜索")
        self._ai_requests.clear()
        self._ocr_requests.clear()
        self._copy_request = None
        if not (self.grid.wait_for_thumbnail_idle() and self.detail.wait_for_thumbnail_idle()):
            self.grid.resume_thumbnail_loader()
            self.detail.resume_thumbnail_loader()
            self._quit_in_progress = False
            self.force_quit = False
            self._set_interactions_enabled(True)
            self.backup_timer.start()
            if self.current_item_id:
                self.update_detail(self.current_item_id)
            QMessageBox.warning(
                self,
                "ClipSave 正在结束缩略图任务",
                "图片预览任务尚未结束，ClipSave 已取消退出。请稍后再次退出。",
            )
            return False
        monitoring_was_active = self.clipboard_service.timer.isActive()
        self.clipboard_service.stop()
        if not self.clipboard_service.wait_for_idle(10.0):
            self.grid.resume_thumbnail_loader()
            self.detail.resume_thumbnail_loader()
            self.clipboard_service.resume_after_failed_shutdown(monitoring_was_active)
            self.backup_timer.start()
            self._quit_in_progress = False
            self.force_quit = False
            self._set_interactions_enabled(True)
            if self.current_item_id:
                self.update_detail(self.current_item_id)
            QMessageBox.warning(
                self,
                "ClipSave 正在保存",
                "仍有剪贴板内容正在写入本地磁盘。为避免数据丢失，ClipSave 暂时不会退出。请稍后再次退出。",
            )
            return False
        try:
            self.database.create_backup()
        except Exception as exc:
            self.grid.resume_thumbnail_loader()
            self.detail.resume_thumbnail_loader()
            self.database.recovery_report["backup_error"] = str(exc)
            self.clipboard_service.resume_after_failed_shutdown(monitoring_was_active)
            self.backup_timer.start()
            self._quit_in_progress = False
            self.force_quit = False
            self._set_interactions_enabled(True)
            if self.current_item_id:
                self.update_detail(self.current_item_id)
            QMessageBox.warning(
                self,
                "数据库备份失败",
                f"退出前无法创建最新数据库备份，ClipSave 已取消退出。\n\n{exc}",
            )
            return False
        persistence_stopped = self.clipboard_service.shutdown()
        if not persistence_stopped:
            self.grid.resume_thumbnail_loader()
            self.detail.resume_thumbnail_loader()
            self.clipboard_service.resume_after_failed_shutdown(monitoring_was_active)
            self.backup_timer.start()
            self._quit_in_progress = False
            self.force_quit = False
            self._set_interactions_enabled(True)
            if self.current_item_id:
                self.update_detail(self.current_item_id)
            QMessageBox.warning(
                self,
                "ClipSave 正在保存",
                "仍有剪贴板内容正在写入本地磁盘。为避免数据丢失，ClipSave 暂时不会退出。请稍后再次退出。",
            )
            return False
        self._closing = True
        self._async_signals.clear()
        shutdown_ai_ocr_task_executor(timeout=2.0)
        self.grid.shutdown_thumbnail_loader()
        self.detail.shutdown_thumbnail_loader()
        if persistence_stopped:
            self.database.close()
        self.tray.hide()
        application = QApplication.instance()
        if application is not None:
            application.exit(0)
        return True

    def closeEvent(self, event: QCloseEvent) -> None:
        tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        if self.force_quit or not self.settings.get("close_to_tray", True) or not tray_available:
            if self.quit_application():
                event.accept()
            else:
                event.ignore()
        else:
            event.ignore()
            self.hide()
            message = "ClipSave 仍在后台保存剪贴板内容。" if self.clipboard_service.timer.isActive() else "ClipSave 已隐藏到托盘，自动捕获当前暂停。"
            self.tray.showMessage("ClipSave", message, QSystemTrayIcon.MessageIcon.Information, 1800)
