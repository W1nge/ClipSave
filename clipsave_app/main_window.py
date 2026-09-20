from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QIcon, QImage, QKeySequence, QShortcut
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
    QPushButton,
    QStackedLayout,
    QStackedWidget,
    QSplitter,
    QSystemTrayIcon,
    QTextEdit,
    QToolTip,
    QVBoxLayout,
    QWidget,
)
from send2trash import send2trash

from .bulk_checkpoint import checkpoint_path
from .bulk_image_controller import BulkImageCompletion, BulkImageController
from .app_paths import AppPaths
from .constants import APP_NAME, LIBRARY_DIR
from .database import LibraryDatabase
from .detail_animation_controller import DetailAnimationController
from .library_controller import LibraryController
from .library_metadata_controller import (
    LibraryMetadataController,
    MetadataMutationResult,
)
from .library_models import LibraryQuery, LibraryViewState
from .maintenance_controller import LibraryMaintenanceController
from .image_task_controller import ImageTaskController
from .monitoring_controller import MonitoringController
from .mutation_controller import LibraryMutationController
from .native_window_controller import NativeWindowController, windows_resize_hit_test
from .shutdown_coordinator import ShutdownCoordinator, ShutdownFailure
from .services import (
    AIService,
    BackdropResult,
    ClipboardService,
    TaskCapacityExceeded,
    ai_ocr_task_executor,
    apply_windows_backdrop,
    preflight_image_file,
    register_windows_power_saving_notification,
    release_windows_backdrop,
    unregister_windows_power_saving_notification,
)
from .settings import Settings
from .sidebar_interaction_controller import SidebarInteractionController
from .startup import set_start_with_windows
from .storage import is_under_local_store, recycle_managed_file
from .styles import stylesheet_for_theme
from .task_supervisor import TaskSupervisor
from .window_effects_controller import WindowEffectsController
from .windows_frame import (
    enable_native_resize_frame,
    handle_getminmaxinfo,
    handle_nccalcsize,
    handle_ncactivate,
    is_windows_qt_platform,
    maximize_native_window,
    native_window_is_maximized,
    restore_native_window,
    synchronize_maximized_work_area,
    window_dpi_scale,
    window_rect,
)
from .widgets import (
    AssetGrid,
    AssetTable,
    BrandLabel,
    CaptureStatusButton,
    CopyToast,
    DateDialog,
    DetailPanel,
    DraggableBar,
    FluentMessageBox as QMessageBox,
    IconButton,
    MarkdownDialog,
    TextDialog,
    SettingsDialog,
    ResizeHandle,
    Sidebar,
    ThemedLineEdit,
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
    ITEM_PAGE_SIZE = 500

    @property
    def _library_refresh_request(self):
        return self.library_controller.refresh_request

    @property
    def _item_search_request(self):
        return self.library_controller.search_request

    @property
    def _item_page_request(self):
        return self.library_controller.page_request

    @property
    def _startup_scan_request(self):
        return self.maintenance_controller.startup_request

    @_startup_scan_request.setter
    def _startup_scan_request(self, value) -> None:
        self.maintenance_controller.startup_request = value

    @property
    def _backup_request(self):
        return self.maintenance_controller.backup_request

    @_backup_request.setter
    def _backup_request(self, value) -> None:
        self.maintenance_controller.backup_request = value

    @property
    def _ai_requests(self):
        return self.image_task_controller.ai_requests

    @property
    def _ocr_requests(self):
        return self.image_task_controller.ocr_requests

    @property
    def _automatic_ai_items(self):
        return self.image_task_controller.automatic_ai_items

    @property
    def _automatic_ocr_items(self):
        return self.image_task_controller.automatic_ocr_items

    @property
    def _expanded_search_request(self):
        return self.image_task_controller.expanded_search_request

    @_expanded_search_request.setter
    def _expanded_search_request(self, value) -> None:
        self.image_task_controller.expanded_search_request = value

    @property
    def _import_request(self):
        return self.mutation_controller.import_request

    @_import_request.setter
    def _import_request(self, value) -> None:
        self.mutation_controller.import_request = value

    @property
    def _copy_request(self):
        return self.mutation_controller.copy_request

    @_copy_request.setter
    def _copy_request(self, value) -> None:
        self.mutation_controller.copy_request = value

    @property
    def _delete_requests(self):
        return self.mutation_controller.delete_requests

    @property
    def _pending_delete_item_ids(self):
        return self.mutation_controller.pending_delete_item_ids

    @property
    def _bulk_image_request(self):
        return self.bulk_image_controller.request

    @property
    def _bulk_image_checkpoint_path(self):
        return self.bulk_image_controller.checkpoint_path

    @property
    def _bulk_image_progress_state(self):
        return self.bulk_image_controller.progress_state

    @_bulk_image_progress_state.setter
    def _bulk_image_progress_state(self, value) -> None:
        self.bulk_image_controller.progress_state = value

    @property
    def _native_backdrop_hwnd(self):
        return self.window_effects_controller.native_backdrop_hwnd

    @_native_backdrop_hwnd.setter
    def _native_backdrop_hwnd(self, value) -> None:
        self.window_effects_controller.native_backdrop_hwnd = value

    @property
    def _native_backdrop_result(self):
        return self.window_effects_controller.native_backdrop_result

    @_native_backdrop_result.setter
    def _native_backdrop_result(self, value) -> None:
        self.window_effects_controller.native_backdrop_result = value

    @property
    def _windows_backdrop_window_hwnd(self):
        return self.window_effects_controller.backdrop_window_hwnd

    @_windows_backdrop_window_hwnd.setter
    def _windows_backdrop_window_hwnd(self, value) -> None:
        self.window_effects_controller.backdrop_window_hwnd = value

    @property
    def _power_saving_notification_hwnd(self):
        return self.window_effects_controller.power_notification_hwnd

    @_power_saving_notification_hwnd.setter
    def _power_saving_notification_hwnd(self, value) -> None:
        self.window_effects_controller.power_notification_hwnd = value

    @property
    def _power_saving_notification_handle(self):
        return self.window_effects_controller.power_notification_handle

    @_power_saving_notification_handle.setter
    def _power_saving_notification_handle(self, value) -> None:
        self.window_effects_controller.power_notification_handle = value

    @property
    def _material_refresh_pending(self) -> bool:
        return self.window_effects_controller.material_refresh_pending

    @_material_refresh_pending.setter
    def _material_refresh_pending(self, value: bool) -> None:
        self.window_effects_controller.material_refresh_pending = bool(value)

    @property
    def _interactive_resize_active(self) -> bool:
        return self.native_window_controller.interactive_resize_active

    @_interactive_resize_active.setter
    def _interactive_resize_active(self, value: bool) -> None:
        self.native_window_controller.interactive_resize_active = bool(value)

    @property
    def _maximized_bounds_sync_pending(self) -> bool:
        return self.native_window_controller.maximized_bounds_sync_pending

    @_maximized_bounds_sync_pending.setter
    def _maximized_bounds_sync_pending(self, value: bool) -> None:
        self.native_window_controller.maximized_bounds_sync_pending = bool(value)

    @property
    def _detail_animation_active(self) -> bool:
        return self.detail_animation_controller.active

    @_detail_animation_active.setter
    def _detail_animation_active(self, value: bool) -> None:
        self.detail_animation_controller.active = bool(value)

    @property
    def _detail_animation_target_visible(self) -> bool:
        return self.detail_animation_controller.target_visible

    @_detail_animation_target_visible.setter
    def _detail_animation_target_visible(self, value: bool) -> None:
        self.detail_animation_controller.target_visible = bool(value)

    @property
    def _detail_animation_progress(self) -> float:
        return self.detail_animation_controller.progress

    @_detail_animation_progress.setter
    def _detail_animation_progress(self, value: float) -> None:
        self.detail_animation_controller.progress = float(value)

    @property
    def _detail_animation_start_progress(self) -> float:
        return self.detail_animation_controller.start_progress

    @_detail_animation_start_progress.setter
    def _detail_animation_start_progress(self, value: float) -> None:
        self.detail_animation_controller.start_progress = float(value)

    @property
    def _detail_animation_end_progress(self) -> float:
        return self.detail_animation_controller.end_progress

    @_detail_animation_end_progress.setter
    def _detail_animation_end_progress(self, value: float) -> None:
        self.detail_animation_controller.end_progress = float(value)

    @property
    def _detail_animation_target_width(self) -> int:
        return self.detail_animation_controller.target_width

    @_detail_animation_target_width.setter
    def _detail_animation_target_width(self, value: int) -> None:
        self.detail_animation_controller.target_width = int(value)

    @property
    def _detail_animation_timer(self):
        return self.detail_animation_controller.timer

    @property
    def _detail_animation_elapsed(self):
        return self.detail_animation_controller.elapsed

    @property
    def _detail_width(self) -> int:
        return self.detail_animation_controller.saved_width

    @_detail_width.setter
    def _detail_width(self, value: int) -> None:
        self.detail_animation_controller.saved_width = int(value)

    @property
    def _sidebar_animation_active(self) -> bool:
        return self.sidebar_interaction_controller.animation_active

    @_sidebar_animation_active.setter
    def _sidebar_animation_active(self, value: bool) -> None:
        self.sidebar_interaction_controller.animation_active = bool(value)

    @property
    def _pending_sidebar_collapsed(self) -> bool | None:
        return self.sidebar_interaction_controller.pending_collapsed

    @_pending_sidebar_collapsed.setter
    def _pending_sidebar_collapsed(self, value: bool | None) -> None:
        self.sidebar_interaction_controller.pending_collapsed = value

    @property
    def _sidebar_setting_timer(self):
        return self.sidebar_interaction_controller.setting_timer

    @property
    def current_items(self):
        return self.library_state.items

    @current_items.setter
    def current_items(self, value) -> None:
        self.library_state.items = value

    @property
    def _items_offset(self) -> int:
        return self.library_state.offset

    @_items_offset.setter
    def _items_offset(self, value: int) -> None:
        self.library_state.offset = value

    @property
    def _items_has_more(self) -> bool:
        return self.library_state.has_more

    @_items_has_more.setter
    def _items_has_more(self, value: bool) -> None:
        self.library_state.has_more = value

    @property
    def _items_loading(self) -> bool:
        return self.library_state.loading

    @_items_loading.setter
    def _items_loading(self, value: bool) -> None:
        self.library_state.loading = value

    @property
    def current_item_id(self) -> int | None:
        return self.library_state.selected_item_id

    @current_item_id.setter
    def current_item_id(self, value: int | None) -> None:
        self.library_state.selected_item_id = value

    @property
    def current_kind(self) -> str | None:
        return self.library_state.kind

    @current_kind.setter
    def current_kind(self, value: str | None) -> None:
        self.library_state.kind = value

    @property
    def current_favorite(self) -> bool:
        return self.library_state.favorite

    @current_favorite.setter
    def current_favorite(self, value: bool) -> None:
        self.library_state.favorite = value

    @property
    def current_day(self) -> str | None:
        return self.library_state.day

    @current_day.setter
    def current_day(self, value: str | None) -> None:
        self.library_state.day = value

    @property
    def current_recent(self) -> bool:
        return self.library_state.recent

    @current_recent.setter
    def current_recent(self, value: bool) -> None:
        self.library_state.recent = value

    @property
    def current_collection(self) -> int | None:
        return self.library_state.collection_id

    @current_collection.setter
    def current_collection(self, value: int | None) -> None:
        self.library_state.collection_id = value

    @property
    def current_tag(self) -> int | None:
        return self.library_state.tag_id

    @current_tag.setter
    def current_tag(self, value: int | None) -> None:
        self.library_state.tag_id = value

    @property
    def current_sort(self) -> str:
        return self.library_state.sort

    @current_sort.setter
    def current_sort(self, value: str) -> None:
        self.library_state.sort = value

    def __init__(
        self,
        database: LibraryDatabase,
        settings: Settings,
        app_icon: QIcon,
        scan_on_start: bool = True,
        reconcile_on_start: bool = False,
        paths: AppPaths | None = None,
        clipboard_service: ClipboardService | None = None,
        runtime=None,
    ):
        super().__init__()
        self.database = database
        self.settings = settings
        self.paths = paths
        self.runtime = runtime
        self.app_icon = app_icon
        self.library_state = LibraryViewState(sort=settings.get("sort", "newest"))
        self.sort_menu = None
        self.sort_menu_closed_at = 0.0
        self.force_quit = False
        self._grid_dirty = True
        self._table_dirty = True
        self._expanded_search_query = ""
        self._expanded_search_terms: tuple[str, ...] = ()
        self._session_hidden_item_ids: set[int] = set()
        self.startup_scan_error: str | None = None
        self._task_supervisor = TaskSupervisor()
        # Compatibility views while callers/tests migrate to TaskSupervisor.
        self._async_tasks = self._task_supervisor.regular_tasks
        self._bounded_tasks = self._task_supervisor.bounded_tasks
        self._async_tasks_lock = self._task_supervisor.lock
        self.library_controller = LibraryController(
            database,
            self._task_supervisor,
            parent=self,
        )
        self.library_metadata_controller = LibraryMetadataController(database)
        self.library_controller.refresh_succeeded.connect(self._library_refresh_succeeded)
        self.library_controller.refresh_failed.connect(self._library_refresh_failed)
        self.library_controller.search_succeeded.connect(self._item_search_succeeded)
        self.library_controller.search_failed.connect(self._item_search_failed)
        self.library_controller.page_succeeded.connect(self._item_page_succeeded)
        self.library_controller.page_failed.connect(self._item_page_failed)
        self.maintenance_controller = LibraryMaintenanceController(
            database,
            self._task_supervisor,
            parent=self,
        )
        self.maintenance_controller.scan_succeeded.connect(self._startup_scan_finished)
        self.maintenance_controller.scan_failed.connect(self._startup_scan_failed)
        self.maintenance_controller.backup_succeeded.connect(self._periodic_backup_finished)
        self.maintenance_controller.backup_failed.connect(self._periodic_backup_failed)
        self.image_task_controller = ImageTaskController(
            self._task_supervisor,
            parent=self,
            start_bounded=lambda token, target, **kwargs: self._start_bounded_task(
                token,
                target,
                **kwargs,
            ),
        )
        self.image_task_controller.ai_succeeded.connect(self._ai_succeeded)
        self.image_task_controller.ai_failed.connect(self._ai_failed)
        self.image_task_controller.ocr_succeeded.connect(self._ocr_succeeded)
        self.image_task_controller.ocr_failed.connect(self._ocr_failed)
        self.image_task_controller.expanded_search_succeeded.connect(
            self._expanded_search_succeeded
        )
        self.image_task_controller.expanded_search_failed.connect(
            self._expanded_search_failed
        )
        self.mutation_controller = LibraryMutationController(
            database,
            self._task_supervisor,
            parent=self,
        )
        self.mutation_controller.import_finished.connect(self._import_finished)
        self.mutation_controller.import_failed.connect(self._import_failed)
        self.mutation_controller.copy_succeeded.connect(self._copy_image_succeeded)
        self.mutation_controller.copy_failed.connect(self._copy_image_failed)
        self.mutation_controller.delete_finished.connect(self._delete_finished)
        self.mutation_controller.delete_failed.connect(self._delete_failed)
        self.bulk_image_controller = BulkImageController(
            database,
            checkpoint_path(Path(settings.path)),
            start_task=lambda token, target: self._start_async_task(token, target),
            cancel_task=lambda token: self._cancel_async_token(token),
            parent=self,
        )
        self.bulk_image_controller.progress_changed.connect(self._bulk_image_progress)
        self.bulk_image_controller.finished.connect(self._bulk_image_finished)
        self._closing = False
        self._quit_in_progress = False
        self.window_effects_controller = WindowEffectsController(
            self,
            platform_check=lambda: is_windows_qt_platform(),
            apply_backdrop=lambda *args, **kwargs: apply_windows_backdrop(*args, **kwargs),
            register_power=lambda hwnd: register_windows_power_saving_notification(hwnd),
            unregister_power=lambda handle: unregister_windows_power_saving_notification(handle),
            sync_surface_style=lambda **kwargs: self._sync_surface_style(**kwargs),
        )
        self.native_window_controller = NativeWindowController(
            self,
            native_events_enabled=lambda: os.name == "nt",
            platform_check=lambda: is_windows_qt_platform(),
            window_rect=lambda hwnd: window_rect(hwnd),
            dpi_scale=lambda hwnd: window_dpi_scale(hwnd),
            handle_getminmaxinfo=lambda *args: handle_getminmaxinfo(*args),
            handle_ncactivate=lambda *args: handle_ncactivate(*args),
            handle_nccalcsize=lambda *args: handle_nccalcsize(*args),
            schedule_material_refresh=lambda: self._schedule_material_refresh(),
            schedule_maximized_bounds_sync=lambda: self._schedule_maximized_bounds_sync(),
            sync_backdrop_from_windowpos=lambda lparam: self._sync_windows_backdrop_from_windowpos(
                lparam
            ),
            sync_backdrop_geometry_now=lambda: self._sync_windows_backdrop_geometry_now(),
            sync_backdrop_window=lambda: self._sync_windows_backdrop_window(),
            schedule_soon=lambda callback: QTimer.singleShot(0, callback),
            set_layout_updates_suspended=lambda value: self.grid.set_layout_updates_suspended(
                value
            ),
            sidebar_animation_active=lambda: self._sidebar_animation_active,
            detail_animation_active=lambda: self._detail_animation_active,
            resize_hit_test=lambda *args: self._windows_resize_hit_test(*args),
            enable_resize_frame=lambda hwnd: enable_native_resize_frame(hwnd),
            clear_resize_handles=lambda: self._clear_resize_handles(),
            install_resize_handles=lambda: (
                self._install_resize_handles(self.centralWidget())
                if not self.resize_handles
                else None
            ),
            native_window_is_maximized=lambda hwnd: native_window_is_maximized(hwnd),
            restore_native_window=lambda hwnd: restore_native_window(hwnd),
            maximize_native_window=lambda hwnd: maximize_native_window(hwnd),
            synchronize_maximized_work_area=lambda hwnd: synchronize_maximized_work_area(
                hwnd
            ),
        )
        self._initial_position_constrained = False
        self.global_hotkey_registered: bool | None = None
        self.dark_theme = self._desired_dark_theme()
        app = QApplication.instance()
        if app is not None:
            app.setProperty("darkTheme", self.dark_theme)

        self.setWindowTitle(APP_NAME)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        if os.name == "nt":
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.resize(1440, 880)
        self.setMinimumSize(800, 440)
        self.setStyleSheet(stylesheet_for_theme(self.dark_theme))
        self.build_ui()
        self.sidebar_interaction_controller = SidebarInteractionController(
            sidebar=self.sidebar,
            grid=self.grid,
            body_layout=self._body_layout,
            settings_get=lambda key, default=None: self.settings.get(key, default),
            save_setting=lambda key, value: self._save_setting(key, value),
            detail_animation_active=lambda: self._detail_animation_active,
            finish_detail_animation=lambda: self._finish_detail_animation(),
            interactive_resize_active=lambda: self._interactive_resize_active,
            parent=self,
        )
        self.sidebar_interaction_controller.connect_signals()
        self.detail_animation_controller = DetailAnimationController(
            self,
            detail=self.detail,
            grid=self.grid,
            content_splitter=self.content_splitter,
            body_layout=self._body_layout,
            detail_button=self.detail_button,
            sidebar=self.sidebar,
            sidebar_animation_active=lambda: self._sidebar_animation_active,
            interactive_resize_active=lambda: self._interactive_resize_active,
            parent=self,
        )
        self.build_tray()
        self.build_shortcuts()
        self._ensure_native_resize_frame()
        self._ensure_power_saving_notification()
        color_scheme_changed = getattr(QApplication.styleHints(), "colorSchemeChanged", None)
        if color_scheme_changed is not None:
            color_scheme_changed.connect(self._system_color_scheme_changed)
        if app is not None:
            app.aboutToQuit.connect(self._release_power_saving_notification)
            app.aboutToQuit.connect(release_windows_backdrop)
            app.aboutToQuit.connect(self._destroy_windows_backdrop_window)

        if clipboard_service is None:
            self.clipboard_service = ClipboardService(database, self, paths=paths)
        else:
            self.clipboard_service = clipboard_service
        self.monitoring_controller = MonitoringController(
            self.settings,
            self.clipboard_service,
        )
        self.clipboard_service.captured.connect(self.on_captured)
        self.clipboard_service.failed.connect(self.show_error_status)
        self.clipboard_service.state_changed.connect(self.update_monitor_button)
        self.shutdown_coordinator = ShutdownCoordinator(
            database,
            self.clipboard_service,
            self.grid,
            self.detail,
            runtime=runtime,
        )
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
        self._sync_surface_style(dark=dark)
        for button in self.findChildren(IconButton):
            button.refresh_theme()
        self.window_title_bar.update_maximize_state(self._window_is_maximized())
        self.sidebar.set_active(getattr(self.sidebar, "active_key", ""))
        self.expanded_search_button.setIcon(lucide_icon("sparkles"))
        self.detail.ai_button.setIcon(lucide_icon("sparkles"))
        self.detail.ocr_button.setIcon(lucide_icon("scan-text"))
        self.grid.viewport().update()
        self.table.viewport().update()
        QTimer.singleShot(
            0,
            lambda: self._apply_native_backdrop(force=True, dark=dark),
        )

    def build_ui(self) -> None:
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(220)
        self.search_timer.timeout.connect(self._refresh_search_items_async)
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
        self._body_layout = body_layout
        root_layout.addWidget(body, 1)

        self.brand_label = BrandLabel("ClipSave", "#21a8fb", 0.86, root)
        self.brand_label.setGeometry(10, 0, 190, 86)
        self.brand_label.raise_()

        self.sidebar = Sidebar()
        self.sidebar.navigation_requested.connect(self.navigate)
        self.sidebar.add_collection_requested.connect(self.add_collection)
        self.sidebar.add_tag_requested.connect(self.add_global_tag)
        self.sidebar.delete_collection_requested.connect(self.delete_collection)
        self.sidebar.delete_tag_requested.connect(self.delete_tag)
        self.sidebar.settings_requested.connect(self.open_settings)
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
        self.search = ThemedLineEdit()
        self.search.setPlaceholderText("搜索剪贴板内容、文件名、标签、OCR 或 AI 描述  (Ctrl+K)")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(560)
        self.search.setMinimumWidth(120)
        self.search.textChanged.connect(self._search_text_changed)
        top_layout.addWidget(self.search, 3)
        self.expanded_search_button = QPushButton("扩大搜索")
        self.expanded_search_button.setIcon(lucide_icon("sparkles"))
        self.expanded_search_button.setToolTip("使用 AI 扩展搜索词，并在本地扩大匹配范围")
        self.expanded_search_button.clicked.connect(self.expand_search)
        top_layout.addWidget(self.expanded_search_button)
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
        self.view_stack = QStackedWidget()
        self.view_stack.setObjectName("ViewStack")
        self.grid = AssetGrid()
        self.grid.verticalScrollBar().valueChanged.connect(
            lambda value: self._load_more_items_if_needed(self.grid, value)
        )
        self.grid.item_selected.connect(self.select_item)
        self.grid.selection_cleared.connect(self.clear_item_selection)
        self.grid.item_activated.connect(self.activate_item)
        self.grid.detail_requested.connect(self.show_item_detail)
        self.grid.open_requested.connect(self.open_item)
        self.grid.favorite_requested.connect(self.set_favorite)
        self.table = AssetTable()
        self.table.verticalScrollBar().valueChanged.connect(
            lambda value: self._load_more_items_if_needed(self.table, value)
        )
        self.table.item_selected.connect(self.select_item)
        self.table.selection_cleared.connect(self.clear_item_selection)
        self.table.item_activated.connect(self.activate_item)
        self.table.detail_requested.connect(self.show_item_detail)
        self.table.open_requested.connect(self.open_item)
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
        self.detail = DetailPanel()
        self.detail.setMaximumWidth(520)
        self.detail.close_requested.connect(self.hide_detail)
        self.detail.copy_requested.connect(self.copy_item)
        self.detail.open_requested.connect(self.open_item)
        self.detail.delete_requested.connect(self.delete_item)
        self.detail.favorite_requested.connect(self.set_favorite)
        self.detail.notes_changed.connect(self.save_notes)
        self.detail.add_tag_requested.connect(self.add_tag_to_item)
        self.detail.remove_tag_requested.connect(self.remove_tag_from_item)
        self.detail.collection_changed.connect(self.set_item_collection)
        self.detail.ai_requested.connect(self.generate_ai_description)
        self.detail.ocr_requested.connect(self.generate_ocr)
        self.content_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.content_splitter.setObjectName("ContentSplitter")
        # A 1 px Qt splitter keeps an expanded grab target overlapping both panes.
        self.content_splitter.setHandleWidth(1)
        self.content_splitter.setChildrenCollapsible(False)
        self.content_splitter.addWidget(self.library_surface)
        self.content_splitter.addWidget(self.detail)
        self.content_splitter.setStretchFactor(0, 1)
        self.content_splitter.setStretchFactor(1, 0)
        self.content_splitter.splitterMoved.connect(self._detail_splitter_moved)
        self.detail.setVisible(False)
        handle = self.content_splitter.handle(1)
        if handle is not None:
            handle.setCursor(Qt.CursorShape.SizeHorCursor)
            handle.setToolTip("调整详情宽度")
        middle_layout.addWidget(self.content_splitter, 1)

        self.set_view_mode(self.settings.get("view_mode", "grid"))
        self.sidebar.set_collapsed(bool(self.settings.get("sidebar_collapsed", False)), animate=False)
        self._create_resize_handles(root)
        self.copy_toast = CopyToast(root)
        self.copy_toast.reposition()

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

    def _clear_resize_handles(self) -> None:
        if not self.resize_handles:
            return
        for handle in self.resize_handles.values():
            handle.deleteLater()
        self.resize_handles = {}

    def _ensure_native_resize_frame(self) -> None:
        self.native_window_controller.ensure_native_resize_frame()

    def _ensure_windows_backdrop_window(self) -> int | None:
        return self.window_effects_controller.ensure_backdrop_window()

    def _destroy_windows_backdrop_window(self) -> None:
        self.window_effects_controller.destroy_backdrop_window()

    def _hide_windows_backdrop_window(self) -> None:
        self.window_effects_controller.hide_backdrop_window()

    def _sync_windows_backdrop_window_rect(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        *,
        visible: bool | None = None,
    ) -> None:
        self.window_effects_controller.sync_window_rect(
            x,
            y,
            width,
            height,
            visible=visible,
        )

    def _sync_windows_backdrop_window(self) -> None:
        self.window_effects_controller.sync_window()

    def _sync_windows_backdrop_geometry_now(self) -> None:
        self.window_effects_controller.sync_geometry_now()

    def _sync_windows_backdrop_from_windowpos(self, lparam: int) -> None:
        self.window_effects_controller.sync_from_windowpos(lparam)

    def _apply_native_backdrop(
        self, *, force: bool = False, dark: bool | None = None
    ) -> None:
        self.window_effects_controller.apply_native_backdrop(
            force=force,
            dark=dark,
        )

    def _ensure_power_saving_notification(self) -> None:
        self.window_effects_controller.ensure_power_notification()

    def _release_power_saving_notification(self) -> None:
        self.window_effects_controller.release_power_notification()

    def _sync_surface_style(
        self,
        *,
        result: BackdropResult | None = None,
        dark: bool | None = None,
    ) -> None:
        effective = result if result is not None else self._native_backdrop_result
        backend = effective.backend.value if effective is not None else None
        stylesheet = stylesheet_for_theme(
            self.dark_theme if dark is None else dark,
            backend=backend,
        )
        if self.styleSheet() != stylesheet:
            self.setStyleSheet(stylesheet)

    def _schedule_material_refresh(self) -> None:
        if self._material_refresh_pending:
            return
        self._material_refresh_pending = True
        QTimer.singleShot(0, self._refresh_material_from_system)

    def _refresh_material_from_system(self) -> None:
        self._material_refresh_pending = False
        if self._closing or self._quit_in_progress:
            return
        if self.settings.get("follow_system_theme", True):
            self.apply_theme(force=True)
            return
        self._apply_native_backdrop(force=True)

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
        if is_windows_qt_platform() and not self._interactive_resize_active:
            self._sync_windows_backdrop_geometry_now()
        if hasattr(self, "copy_toast"):
            self.copy_toast.reposition()

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        if is_windows_qt_platform() and not self._interactive_resize_active:
            self._sync_windows_backdrop_geometry_now()

    def toggle_maximized(self) -> None:
        self.native_window_controller.toggle_maximized()
        self.window_title_bar.update_maximize_state(self._window_is_maximized())
        QTimer.singleShot(
            0,
            lambda: self.window_title_bar.update_maximize_state(
                self._window_is_maximized()
            ),
        )

    def _window_is_maximized(self) -> bool:
        return self.native_window_controller.window_is_maximized()

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self, "window_title_bar"):
            self.window_title_bar.update_maximize_state(self._window_is_maximized())
            self._update_resize_handles()
            if is_windows_qt_platform():
                if self.isMinimized():
                    self._hide_windows_backdrop_window()
                else:
                    QTimer.singleShot(0, self._sync_windows_backdrop_window)
        super().changeEvent(event)

    def nativeEvent(self, event_type, message):
        handled = self.native_window_controller.handle_native_event(event_type, message)
        if handled is not None:
            return handled
        return super().nativeEvent(event_type, message)

    def _schedule_maximized_bounds_sync(self) -> None:
        if self._maximized_bounds_sync_pending:
            return
        self._maximized_bounds_sync_pending = True
        QTimer.singleShot(0, self._synchronize_maximized_bounds)

    def _synchronize_maximized_bounds(self) -> None:
        self._maximized_bounds_sync_pending = False
        if self._closing or self._quit_in_progress or not is_windows_qt_platform():
            return
        self.native_window_controller.sync_maximized_work_area()

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
        return windows_resize_hit_test(
            x,
            y,
            left,
            top,
            right,
            bottom,
            device_pixel_ratio,
            edge_width=cls.RESIZE_EDGE_WIDTH,
            corner_size=cls.RESIZE_CORNER_SIZE,
        )

    def _begin_interactive_resize(self) -> None:
        self.native_window_controller.begin_interactive_resize()

    def _end_interactive_resize(self) -> None:
        self.native_window_controller.end_interactive_resize()

    def _begin_sidebar_animation(self) -> None:
        self.sidebar_interaction_controller.begin_animation()

    def _update_sidebar_animation(self, progress: float) -> None:
        self.sidebar_interaction_controller.update_animation(progress)

    def _end_sidebar_animation(self) -> None:
        self.sidebar_interaction_controller.end_animation()

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
            QShortcut(QKeySequence("Delete"), self, activated=self._delete_focused_text_or_item),
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

    def _delete_focused_text_or_item(self) -> None:
        focused = QApplication.focusWidget()
        if isinstance(focused, QLineEdit):
            focused.del_()
            return
        if isinstance(focused, QTextEdit):
            cursor = focused.textCursor()
            cursor.deleteChar()
            focused.setTextCursor(cursor)
            return
        if self.current_item_id:
            self.delete_item(self.current_item_id)

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

    def _apply_navigation_metadata(self, counts, collections, tags) -> None:
        self.sidebar.set_primary(counts)
        self.sidebar.set_collections(collections)
        self.sidebar.set_tags(tags)
        self.detail.set_collections(collections)

    def _search_text_changed(self, _text: str) -> None:
        self._expanded_search_query = ""
        self._expanded_search_terms = ()
        self._cancel_item_search_request()
        self._cancel_item_page_request()
        self._cancel_expanded_search_request()
        self.search_timer.start()

    def _expanded_search_active(self) -> bool:
        return bool(
            self._expanded_search_terms
            and self._expanded_search_query == self.search.text().strip()
        )

    def _refresh_navigation_metadata(self) -> None:
        counts = self.database.counts()
        collections = self.database.collections()
        self._apply_navigation_metadata(counts, collections, self.database.tags())

    def _refresh_library_async(self) -> None:
        if self._closing or self._quit_in_progress:
            return
        self.library_controller.refresh(
            self._current_item_query_spec(),
            self.ITEM_PAGE_SIZE,
        )

    def _library_refresh_succeeded(
        self,
        token: object,
        spec: LibraryQuery,
        payload: object,
    ) -> None:
        if not self.library_controller.finish_refresh(token):
            return
        if self._closing or self._quit_in_progress or not isinstance(payload, dict):
            return
        self._apply_navigation_metadata(
            payload.get("counts", {}),
            payload.get("collections", []),
            payload.get("tags", []),
        )
        if spec != self._current_item_query_spec():
            return
        items = payload.get("items")
        if not isinstance(items, list):
            self.show_error_status("资料库刷新失败：返回结果无效")
            return
        self._items_offset = 0
        self._items_loading = False
        self._apply_first_page(items)

    def _library_refresh_failed(
        self,
        token: object,
        message: str,
    ) -> None:
        if not self.library_controller.finish_refresh(token):
            return
        if not self._closing and not self._quit_in_progress:
            self.show_error_status(f"资料库刷新失败：{message}")

    def _cancel_library_refresh_request(self) -> None:
        self.library_controller.cancel_refresh()


    def refresh_items(self) -> None:
        self._cancel_item_search_request()
        self._cancel_item_page_request()
        self._items_offset = 0
        self._items_loading = False
        items = self._query_current_items(self.ITEM_PAGE_SIZE, 0)
        self._apply_first_page(items)

    def _apply_first_page(self, items) -> None:
        self._items_offset = len(items)
        self._items_has_more = len(items) == self.ITEM_PAGE_SIZE
        self._apply_items(items)
        expanded_suffix = " · 已扩大搜索" if self._expanded_search_active() else ""
        self.result_count.setText(
            f"{len(self.current_items):,}{'+' if self._items_has_more else ''} 项{expanded_suffix}"
        )
        filters = []
        if self.current_day:
            filters.append(self.current_day)
        if self.search.text().strip():
            label = f"搜索：{self.search.text().strip()}"
            if self._expanded_search_active():
                label += "（已扩大）"
            filters.append(label)
        self.filter_hint.setText("  ·  ".join(filters))

    def _current_item_query_spec(self) -> LibraryQuery:
        expanded_terms = (
            self._expanded_search_terms if self._expanded_search_active() else None
        )
        return LibraryQuery(
            query=self.search.text().strip(),
            query_terms=expanded_terms,
            kind=self.current_kind,
            favorite=self.current_favorite,
            day=self.current_day,
            recent_days=7 if self.current_recent else None,
            collection_id=self.current_collection,
            tag_id=self.current_tag,
            sort=self.current_sort,
        )

    def _query_items_for_spec(
        self,
        spec: LibraryQuery,
        limit: int,
        offset: int,
    ):
        return self.library_controller.query_items(spec, limit, offset)

    def _query_current_items(self, limit: int, offset: int):
        return self._query_items_for_spec(
            self._current_item_query_spec(),
            limit,
            offset,
        )

    def _refresh_search_items_async(self) -> None:
        if self._closing or self._quit_in_progress:
            return
        self.library_controller.search(
            self._current_item_query_spec(),
            self.ITEM_PAGE_SIZE,
        )

    def _item_search_succeeded(
        self,
        token: object,
        spec: LibraryQuery,
        items: object,
    ) -> None:
        if not self.library_controller.finish_search(token):
            return
        if self._closing or self._quit_in_progress:
            return
        if spec != self._current_item_query_spec():
            return
        if not isinstance(items, list):
            self.show_error_status("搜索失败：返回结果无效")
            return
        self._items_offset = 0
        self._items_loading = False
        self._apply_first_page(items)

    def _item_search_failed(
        self,
        token: object,
        message: str,
    ) -> None:
        if not self.library_controller.finish_search(token):
            return
        if not self._closing and not self._quit_in_progress:
            self.show_error_status(f"搜索失败：{message}")

    def _cancel_item_search_request(self) -> None:
        self.library_controller.cancel_search()

    def _cancel_item_page_request(self) -> None:
        self.library_controller.cancel_page()
        self._items_loading = False

    def _load_more_items_if_needed(self, view, value: int) -> None:
        if value < view.verticalScrollBar().maximum() - 120:
            return
        self.load_more_items()

    def load_more_items(self) -> None:
        if (
            self._items_loading
            or self._item_search_request is not None
            or self._item_page_request is not None
            or not self._items_has_more
            or self._closing
            or self._quit_in_progress
        ):
            return
        self._items_loading = True
        spec = self._current_item_query_spec()
        offset = self._items_offset
        self.library_controller.load_page(spec, offset, self.ITEM_PAGE_SIZE)

    def _item_page_succeeded(
        self,
        token: object,
        spec: LibraryQuery,
        offset: int,
        items: object,
    ) -> None:
        if not self.library_controller.finish_page(token):
            return
        self._items_loading = False
        if self._closing or self._quit_in_progress:
            return
        if (
            spec != self._current_item_query_spec()
            or offset != self._items_offset
            or not isinstance(items, list)
        ):
            return
        self._items_offset += len(items)
        self._items_has_more = len(items) == self.ITEM_PAGE_SIZE
        if items:
            existing_ids = {item["id"] for item in self.current_items}
            self._apply_items(
                self.current_items
                + [item for item in items if item["id"] not in existing_ids]
            )
        expanded_suffix = " · 已扩大搜索" if self._expanded_search_active() else ""
        self.result_count.setText(
            f"{len(self.current_items):,}{'+' if self._items_has_more else ''} 项{expanded_suffix}"
        )

    def _item_page_failed(
        self,
        token: object,
        message: str,
    ) -> None:
        if not self.library_controller.finish_page(token):
            return
        self._items_loading = False
        if not self._closing and not self._quit_in_progress:
            self.show_error_status(f"加载更多失败：{message}")
            self._items_loading = False

    def _apply_items(self, items) -> None:
        self.current_items = [
            item
            for item in items
            if item["id"] not in self._session_hidden_item_ids
            and item["id"] not in self._pending_delete_item_ids
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
        self._refresh_search_items_async()

    def open_day(self, day: str) -> None:
        self.current_kind = None
        self.current_favorite = False
        self.current_collection = None
        self.current_tag = None
        self.current_day = day
        self.current_recent = False
        self.page_title.setText(f"{day} 的内容")
        self.sidebar.set_active("date")
        self._refresh_search_items_async()

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
        if self._detail_animation_target_visible:
            self.hide_detail()
        else:
            if self.current_item_id:
                self.update_detail(self.current_item_id)
            self._start_detail_animation(True)

    def show_item_detail(self, item_id: int) -> None:
        if (
            self._detail_animation_target_visible
            and self.current_item_id == item_id
        ):
            self.hide_detail()
            return
        self.select_item(item_id)
        if self.current_item_id != item_id:
            return
        self.update_detail(item_id)
        self._start_detail_animation(True)

    def hide_detail(self) -> None:
        self._start_detail_animation(False)

    def _desired_detail_width(self) -> int:
        return self.detail_animation_controller.desired_width()

    def _start_detail_animation(self, visible: bool) -> None:
        self.detail_animation_controller.start(visible)

    def _advance_detail_animation(self) -> None:
        self.detail_animation_controller.advance()

    def _set_detail_animation_progress(self, progress: float) -> None:
        self.detail_animation_controller.set_progress(progress)

    def _apply_detail_panel_width(self, width: int) -> None:
        self.detail_animation_controller.apply_panel_width(width)

    def _finish_detail_animation(self) -> None:
        self.detail_animation_controller.finish()

    def _detail_splitter_moved(self, _position: int, _index: int) -> None:
        self.detail_animation_controller.splitter_moved(_position, _index)

    def _restore_detail_splitter_size(self) -> None:
        self.detail_animation_controller.restore_splitter_size()

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
        self._refresh_search_items_async()

    def toggle_monitor(self) -> None:
        result = self.monitoring_controller.toggle()
        if not result.succeeded:
            self.show_error_status(result.message)
            return
        self._show_monitor_notification(result.active)

    def _show_monitor_notification(self, active: bool) -> None:
        message = "本地自动捕获已开启" if active else "本地自动捕获已暂停"
        self.show_status(message)
        button = self.capture_status
        QToolTip.showText(
            button.mapToGlobal(button.rect().bottomLeft()),
            message,
            button,
            QRect(),
            2800,
        )

    def _save_setting(self, key: str, value) -> None:
        try:
            self.settings.set(key, value)
        except OSError as exc:
            self.show_error_status(f"设置无法保存：{exc}")

    def _queue_sidebar_collapsed_setting(self, value: bool) -> None:
        self.sidebar_interaction_controller.queue_collapsed_setting(value)

    def _flush_pending_sidebar_collapsed_setting(self, force: bool = False) -> None:
        self.sidebar_interaction_controller.flush_collapsed_setting(force=force)

    def update_monitor_button(self, active: bool) -> None:
        self.capture_status.set_active(active)
        if hasattr(self, "tray_monitor_action"):
            self.tray_monitor_action.setText("暂停监听" if active else "继续监听")
            self.tray.setToolTip("ClipSave - " + ("正在监听剪贴板" if active else "监听已暂停"))

    def on_captured(self, _item_id: int) -> None:
        if self._closing or self._quit_in_progress:
            return
        self.show_status("已保存一条新的剪贴板内容")
        self._refresh_library_async()
        self._schedule_auto_image_tasks(_item_id)

    def activate_item(self, item_id: int) -> None:
        item = self.database.get_item(item_id)
        if not item:
            return
        if item["kind"] == "markdown":
            self._exec_transient_dialog(
                MarkdownDialog(item["title"], item["content"], item["path"], self)
            )
        else:
            self.copy_item(item_id)

    def open_item(self, item_id: int) -> None:
        item = self.database.get_item(item_id)
        if not item:
            return
        if item["kind"] == "markdown":
            self._exec_transient_dialog(
                MarkdownDialog(item["title"], item["content"], item["path"], self)
            )
        elif item["kind"] == "text":
            self._exec_transient_dialog(
                TextDialog(item["title"], item["content"], self)
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
            try:
                self.mutation_controller.start_copy_image(item_id, snapshot)
            except TaskCapacityExceeded as exc:
                QMessageBox.warning(self, "复制任务繁忙", str(exc))
            return
        else:
            text = item["content"]
            self.clipboard_service.suppress_text(text)
            clipboard.setText(text)
        self._show_copy_confirmation()

    def _copy_image_succeeded(self, token: object, signals: AsyncSignals, item_id: int, image: QImage) -> None:
        if not self.mutation_controller.finish_copy(token, signals):
            return
        if self._closing:
            return
        self.clipboard_service.suppress_image(image)
        QApplication.clipboard().setImage(image)
        self._show_copy_confirmation()

    def _show_copy_confirmation(self) -> None:
        self.show_status("已复制到剪贴板")
        self.copy_toast.show_confirmation()

    def _copy_image_failed(self, token: object, signals: AsyncSignals, message: str) -> None:
        if not self.mutation_controller.finish_copy(token, signals):
            return
        if self._closing:
            return
        QMessageBox.warning(self, "复制失败", message)
        self.show_status("复制失败：图片文件无法读取")

    def delete_item(self, item_id: int) -> None:
        if self._closing or self._quit_in_progress:
            return
        if item_id in self._delete_requests:
            self.show_status("该内容正在删除")
            return
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
        item_snapshot = dict(item)
        was_selected = self.current_item_id == item_id
        detail_was_visible = self.detail.isVisible()
        library_root = self.paths.library_dir if self.paths is not None else LIBRARY_DIR
        request = self.mutation_controller.start_delete(
            item_snapshot,
            was_selected=was_selected,
            detail_was_visible=detail_was_visible,
            is_managed=lambda path: is_under_local_store(path),
            recycle=lambda path, root, **kwargs: recycle_managed_file(
                path,
                root,
                send2trash,
                **kwargs,
            ),
            library_root=library_root,
        )
        self._cancel_item_requests(item_id)
        if was_selected:
            self.current_item_id = None
            self.grid.clear_selection()
            self.table.clear_selected_item()
            self.detail.clear_item()
        self._apply_items(self.current_items)
        self.show_status("正在删除内容…")
        if item_id not in self._delete_requests:
            self._delete_failed(request[0], request[1], item_id, "删除任务无法启动")

    def _restore_delete_view(self, item_id: int, was_selected: bool, detail_was_visible: bool) -> None:
        # Failure/cancellation recovery is rare and must restore selection against
        # the refreshed model before select_item() runs.
        self.refresh_library()
        if not was_selected or self.current_item_id is not None:
            return
        if self.database.get_item(item_id) is None:
            return
        self.select_item(item_id)
        if detail_was_visible:
            self.update_detail(item_id)
            if not self._detail_animation_target_visible:
                self._start_detail_animation(True)

    def _delete_finished(
        self, token: object, signals: AsyncSignals, item_id: int, result: object
    ) -> None:
        restore = self.mutation_controller.finish_delete(item_id, token, signals)
        if restore is None:
            return
        if self._closing:
            return
        details = result if isinstance(result, dict) else {"outcome": "deleted"}
        outcome = details.get("outcome")
        was_selected = restore.was_selected
        detail_was_visible = restore.detail_was_visible
        if outcome == "cancelled":
            self._restore_delete_view(item_id, was_selected, detail_was_visible)
            self.show_status("删除已取消")
            return
        if outcome == "reconciled":
            if details.get("mark_error"):
                self._session_hidden_item_ids.add(item_id)
            self._cancel_item_requests(item_id)
            self._refresh_after_mutation()
            message = (
                "文件已移入回收站，但 ClipSave 索引删除失败。"
                "该条目已在当前会话隐藏，并会在下次启动时重新核对。"
                f"\n\n{details.get('error', '')}"
            )
            if details.get("mark_error"):
                message += f"\n\n标记缺失也失败：{details['mark_error']}"
            QMessageBox.warning(self, "删除失败", message)
            self.show_status("文件已移入回收站，失效条目已隐藏")
            return
        if outcome == "failed":
            self._restore_delete_view(item_id, was_selected, detail_was_visible)
            if details.get("stage") == "recycle":
                message = f"文件无法移入回收站，内容未删除。\n\n{details.get('error', '')}"
                status = "删除失败：文件未能移入回收站"
            else:
                message = f"ClipSave 索引删除失败，内容未删除。\n\n{details.get('error', '')}"
                status = "删除失败：内容索引未能移除"
            QMessageBox.warning(self, "删除失败", message)
            self.show_status(status)
            return
        self._cancel_item_requests(item_id)
        if was_selected and self.current_item_id is None:
            self.hide_detail()
        self._refresh_after_mutation()
        if details.get("managed_file") and details.get("file_exists"):
            status = "内容及文件已移入回收站"
        elif details.get("managed_file"):
            status = "内容索引已移除，文件已不存在"
        elif self.database.get_item(item_id) is None and self.current_item_id is None:
            status = "内容索引已移除，原文件未删除" if details.get("file_exists") else "内容已删除"
        else:
            status = "内容已删除"
        self.show_status(status)

    def _delete_failed(self, token: object, signals: AsyncSignals, item_id: int, message: str) -> None:
        restore = self.mutation_controller.finish_delete(item_id, token, signals)
        if restore is None:
            return
        if self._closing:
            return
        self._restore_delete_view(
            item_id,
            restore.was_selected,
            restore.detail_was_visible,
        )
        QMessageBox.warning(self, "删除失败", f"文件或内容无法删除，内容未删除。\n\n{message}")
        self.show_status("删除失败：内容未能删除")

    def set_favorite(self, item_id: int, value: bool) -> None:
        if not self._handle_metadata_result(
            self.library_metadata_controller.set_favorite(item_id, value),
            "收藏更新失败",
            "收藏状态未保存",
        ):
            return
        self._refresh_after_mutation()
        if self.detail.isVisible() and self.current_item_id == item_id:
            self.update_detail(item_id)

    def save_notes(self, item_id: int, notes: str) -> bool:
        if not self._handle_metadata_result(
            self.library_metadata_controller.set_notes(item_id, notes),
            "备注保存失败",
            "备注未保存",
        ):
            return False
        self.detail.mark_notes_saved(item_id, notes)
        if self.search.text().strip():
            self.refresh_items()
        self.show_status("备注已保存")
        return True

    def add_collection(self) -> None:
        name, ok = QInputDialog.getText(self, "新建集合", "集合名称")
        if ok and name.strip():
            if not self._handle_metadata_result(
                self.library_metadata_controller.create_collection(name.strip()),
                "集合创建失败",
                "集合未创建",
            ):
                return
            self._refresh_after_mutation()

    def add_global_tag(self) -> None:
        if not self.current_item_id:
            QMessageBox.information(self, "添加标签", "请先选择一项内容。")
            return
        self.add_tag_to_item(self.current_item_id)

    def delete_collection(self, collection_id: int, name: str) -> None:
        answer = QMessageBox.question(
            self,
            "删除集合",
            f"确定删除集合“{name}”吗？\n\n集合中的剪贴板内容不会被删除，而会移至“未分类”。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if not self._handle_metadata_result(
            self.library_metadata_controller.delete_collection(collection_id),
            "集合删除失败",
            "集合未删除",
        ):
            return
        self._refresh_after_classification_delete(
            active=self.current_collection == collection_id
        )
        self.show_status("集合已删除")

    def delete_tag(self, tag_id: int, name: str) -> None:
        answer = QMessageBox.question(
            self,
            "删除标签",
            f"确定删除标签“{name}”吗？\n\n只会删除标签及其关联，剪贴板内容不会被删除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if not self._handle_metadata_result(
            self.library_metadata_controller.delete_tag(tag_id),
            "标签删除失败",
            "标签未删除",
        ):
            return
        self._refresh_after_classification_delete(active=self.current_tag == tag_id)
        self.show_status("标签已删除")

    def _refresh_after_classification_delete(self, active: bool) -> None:
        if active:
            self._refresh_navigation_metadata()
            self.navigate("all", None)
        else:
            self._refresh_after_mutation()
        if self.detail.isVisible() and self.current_item_id is not None:
            self.update_detail(self.current_item_id)

    def add_tag_to_item(self, item_id: int) -> None:
        name, ok = QInputDialog.getText(self, "添加标签", "标签名称")
        if ok and name.strip():
            if not self._handle_metadata_result(
                self.library_metadata_controller.add_tag(item_id, name.strip()),
                "标签添加失败",
                "标签未添加",
            ):
                return
            self._refresh_after_mutation()
            self.update_detail(item_id)

    def remove_tag_from_item(self, item_id: int, name: str) -> None:
        if not self._handle_metadata_result(
            self.library_metadata_controller.remove_tag(item_id, name),
            "标签移除失败",
            "标签未移除",
        ):
            return
        self._refresh_after_mutation()
        if self.current_item_id == item_id:
            self.update_detail(item_id)

    def set_item_collection(self, item_id: int, collection_id) -> None:
        if not self._handle_metadata_result(
            self.library_metadata_controller.set_collection(item_id, collection_id),
            "集合更新失败",
            "集合未更新",
        ):
            if self.current_item_id == item_id:
                self.update_detail(item_id)
            return
        self._refresh_after_mutation()
        self.show_status("集合已更新")

    def _refresh_after_mutation(self) -> None:
        self._refresh_library_async()

    def _handle_metadata_result(
        self,
        result: MetadataMutationResult,
        title: str,
        status: str,
    ) -> bool:
        if result.succeeded:
            return True
        QMessageBox.warning(self, title, str(result.error))
        self.show_status(status)
        return False

    def import_files(self, parent=None) -> None:
        if self._closing or self._quit_in_progress:
            return
        if self._import_request is not None:
            QMessageBox.information(self, "正在导入", "上一批文件仍在导入，请稍候。")
            return
        paths, _ = QFileDialog.getOpenFileNames(parent or self, "导入内容", "", "支持的文件 (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.md)")
        if not paths:
            return
        self.show_status(f"正在导入 {len(paths)} 个文件")
        try:
            self.mutation_controller.start_import(paths)
        except Exception as exc:
            QMessageBox.warning(self, "文件导入失败", str(exc))

    def _import_finished(self, token: object, signals: AsyncSignals, result: dict) -> None:
        if not self.mutation_controller.finish_import(token, signals):
            return
        if self._closing or self._quit_in_progress:
            return
        self._refresh_library_async()
        for item_id in result.get("image_ids", ()):
            self._schedule_auto_image_tasks(int(item_id))
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
        if not self.mutation_controller.finish_import(token, signals):
            return
        if self._closing or self._quit_in_progress:
            return
        QMessageBox.warning(self, "文件导入失败", message)

    def open_settings(self) -> None:
        previous_follow_system = self.settings.get("follow_system_theme", True)
        previous_theme_mode = self.settings.get("theme_mode", "light")
        previous_start_with_windows = self.settings.get("start_with_windows", False)
        previous_auto_ocr = self.settings.get("auto_ocr", False)
        previous_auto_description = self.settings.get("auto_description", False)
        dialog = SettingsDialog(self.settings, self)
        dialog.import_requested.connect(lambda: self.import_files(dialog))
        dialog.bulk_processing_requested.connect(
            lambda: self._confirm_bulk_image_processing(dialog)
        )
        result = self._exec_transient_dialog(dialog)
        if result:
            start_with_windows = self.settings.get("start_with_windows", False)
            if start_with_windows != previous_start_with_windows:
                try:
                    set_start_with_windows(start_with_windows)
                except OSError as exc:
                    try:
                        self.settings.set("start_with_windows", previous_start_with_windows)
                    except OSError:
                        pass
                    QMessageBox.warning(self, "开机自启动设置失败", str(exc))
        if result and (
            self.settings.get("follow_system_theme", True) != previous_follow_system
            or self.settings.get("theme_mode", "light") != previous_theme_mode
        ):
            self.apply_theme(force=True)
        if result:
            if previous_auto_ocr and not self.settings.get("auto_ocr", False):
                self._cancel_automatic_requests("ocr")
            if previous_auto_description and not self.settings.get("auto_description", False):
                self._cancel_automatic_requests("ai")

    @staticmethod
    def _bulk_progress_state(
        checkpoint,
        *,
        active: bool = False,
        phase: str = "",
        error: str = "",
    ) -> dict[str, object]:
        return BulkImageController.progress_state_for(
            checkpoint,
            active=active,
            phase=phase,
            error=error,
        )

    def _initial_bulk_image_progress_state(self) -> dict[str, object]:
        return self.bulk_image_controller.snapshot()

    def bulk_image_progress_snapshot(self) -> dict[str, object]:
        return self.bulk_image_controller.snapshot()

    def _load_bulk_image_checkpoint(self):
        return self.bulk_image_controller.load_checkpoint()

    def _confirm_bulk_image_processing(self, dialog: SettingsDialog) -> None:
        if self._bulk_image_request is not None:
            QMessageBox.information(self, "批量处理进行中", "当前已有批量图片处理任务正在运行。")
            return
        base_url = dialog.base_url.text().strip()
        vision_model = dialog.vision_model.text().strip()
        if not base_url or not vision_model:
            QMessageBox.warning(
                dialog,
                "AI 服务未配置",
                "请先填写 Base URL 和视觉模型名称，再开始批量处理。",
            )
            return
        checkpoint = self._load_bulk_image_checkpoint()
        if checkpoint is not None and checkpoint.processed >= checkpoint.total:
            checkpoint = None
        image_count = (
            checkpoint.total
            if checkpoint is not None
            else self.database.count_items(kind="image")
        )
        if not image_count:
            QMessageBox.information(dialog, "没有图片", "本地资料库中没有可处理的图片。")
            return
        if checkpoint is not None:
            confirmation = (
                f"将从 {checkpoint.processed:,}/{checkpoint.total:,} 继续，"
                f"当前阶段为{'生成描述' if checkpoint.stage == 'description' else 'OCR'}。\n\n"
                "已经完成的图片不会重复请求。确定继续吗？"
            )
        else:
            confirmation = (
                f"将对现有 {image_count:,} 张图片重新执行 OCR 并生成描述，"
                "已有 OCR 和描述会被覆盖。\n\n"
                f"需要发送约 {image_count * 2:,} 次视觉请求，可能产生费用。确定继续吗？"
            )
        answer = QMessageBox.question(
            dialog,
            "继续批量处理图片" if checkpoint is not None else "确认批量处理图片",
            confirmation,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if not dialog.persist_bulk_configuration():
            return
        dialog.bulk_processing_confirmed = True
        self.start_bulk_image_processing()
        dialog._refresh_bulk_progress()

    def start_bulk_image_processing(self) -> None:
        if self._closing or self._quit_in_progress:
            return
        if self._bulk_image_request is not None:
            QMessageBox.information(self, "批量处理进行中", "当前已有批量图片处理任务正在运行。")
            return
        service = self._ai_service()
        if not service.configured:
            QMessageBox.warning(
                self,
                "AI 服务未配置",
                "请先在设置中填写 Base URL 和视觉模型名称。",
            )
            return
        try:
            checkpoint = self.bulk_image_controller.start(service)
        except LookupError as exc:
            if str(exc) == "NO_IMAGES":
                QMessageBox.information(self, "没有图片", "本地资料库中没有可处理的图片。")
                return
            raise
        except Exception as exc:
            QMessageBox.warning(self, "批量处理无法启动", str(exc))
            return
        self.show_status(
            f"批量处理已开始：从 {checkpoint.processed:,}/{checkpoint.total:,} 继续"
        )

    def _bulk_image_progress(
        self,
        processed: int,
        total: int,
        item_id: int,
        phase: str,
    ) -> None:
        if self._closing:
            return
        if self.current_item_id == item_id:
            self.update_detail(item_id)
        self.show_status(f"批量处理图片 {processed:,}/{total:,}：{phase}")

    def _bulk_image_finished(
        self,
        completion: object,
    ) -> None:
        if not isinstance(completion, BulkImageCompletion):
            return
        details = completion.details
        error = completion.error
        if self._closing or self._quit_in_progress:
            return
        self._refresh_library_async()
        if error:
            QMessageBox.warning(
                self,
                "批量处理已停止",
                (
                    f"服务请求失败，批量任务已停止。\n\n{error}\n\n"
                    f"已完成 {int(details.get('completed', 0)):,} 张，"
                    f"跳过 {int(details.get('skipped', 0)):,} 张，"
                    f"本地文件失败 {int(details.get('failed', 0)):,} 张。\n\n"
                    "修复服务问题后，可在设置中从断点继续。"
                ),
            )
            return
        if details.get("cancelled"):
            self.show_status("批量图片处理已取消")
            return
        self.show_status(
            f"批量处理完成：成功 {int(details.get('completed', 0)):,} 张，"
            f"跳过 {int(details.get('skipped', 0)):,} 张，"
            f"失败 {int(details.get('failed', 0)):,} 张"
        )

    def _start_async_task(self, token: object, target) -> threading.Event:
        return self._task_supervisor.start_thread(token, target)

    def _start_bounded_task(self, token: object, target, *, estimated_bytes: int = 0):
        handle = ai_ocr_task_executor().submit(target, estimated_bytes=estimated_bytes)
        self._task_supervisor.track_bounded(token, handle)
        return handle

    def _ai_service(self) -> AIService:
        return AIService(
            self.settings.get("ai_base_url", ""),
            self.settings.get("ai_api_key", ""),
            self.settings.get("ai_vision_model", ""),
        )

    @staticmethod
    def _image_task_estimate(item) -> int:
        try:
            width = max(0, int(item["width"] or 0))
            height = max(0, int(item["height"] or 0))
            return width * height * 4
        except (KeyError, TypeError, ValueError):
            return 0

    def _cancel_automatic_requests(self, operation: str) -> None:
        for item_id in self.image_task_controller.cancel_automatic(operation):
            if self.current_item_id != item_id:
                continue
            if operation == "ai":
                self.detail.set_ai_busy(False)
            else:
                self.detail.set_ocr_busy(False)

    def _schedule_auto_image_tasks(self, item_id: int) -> None:
        if self._closing or self._quit_in_progress:
            return
        item = self.database.get_item(item_id)
        if not item or item["kind"] != "image" or not item["path"]:
            return
        service = self._ai_service()
        if not service.configured:
            return
        if (
            self.settings.get("auto_ocr", False)
            and not str(item["ocr_text"] or "").strip()
            and item_id not in self._ocr_requests
        ):
            self.generate_ocr(item_id, automatic=True)
        if (
            self.settings.get("auto_description", False)
            and not str(item["ai_description"] or "").strip()
            and item_id not in self._ai_requests
        ):
            self.generate_ai_description(item_id, automatic=True)

    def _start_startup_scan(self, full_scan: bool, reconcile_images: bool) -> None:
        self.startup_scan_error = None
        self.maintenance_controller.start_scan(full_scan, reconcile_images)

    def _start_periodic_backup(self) -> None:
        if self._closing or self._quit_in_progress or self._backup_request is not None:
            return
        if not self.database.backup_state()["dirty"]:
            return
        self.maintenance_controller.start_backup()

    def _periodic_backup_finished(self, token: object, signals: QObject, _path: str) -> None:
        self.maintenance_controller.finish_backup(token, signals)

    def _periodic_backup_failed(self, token: object, signals: QObject, message: str) -> None:
        if not self.maintenance_controller.finish_backup(token, signals) or self._closing:
            return
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

    def _startup_scan_finished(self, token: object, signals: QObject, imported: int) -> None:
        if not self.maintenance_controller.finish_scan(token, signals) or self._closing:
            return
        self.startup_scan_error = None
        self._refresh_library_async()
        if imported:
            self.show_status(f"已导入 {imported} 个现有文件")
        report = getattr(self.database, "last_scan_report", {})
        if report.get("failed"):
            self.show_error_status(
                f"启动扫描跳过了 {report['failed']} 个文件；已处理 {report.get('scanned', 0)} 个"
            )

    def _startup_scan_failed(self, token: object, signals: QObject, message: str) -> None:
        if not self.maintenance_controller.finish_scan(token, signals) or self._closing:
            return
        self.startup_scan_error = message
        self.show_error_status(f"启动扫描失败：{message}")

    def _cancel_async_token(self, token: object) -> None:
        self._task_supervisor.cancel(token)

    def _finish_async_token(self, token: object) -> None:
        self._task_supervisor.finish_bounded(token)

    def _schedule_cancelled_request_cleanup(self, cancelled_tokens: set[object]) -> None:
        if not cancelled_tokens:
            return

        def poll() -> None:
            if self._closing:
                return
            image_cleanup = self.image_task_controller.cleanup_cancelled(cancelled_tokens)
            mutation_cleanup = self.mutation_controller.cleanup_cancelled(cancelled_tokens)

            if self.current_item_id in image_cleanup.ai_item_ids:
                self.detail.set_ai_busy(False)
            if self.current_item_id in image_cleanup.ocr_item_ids:
                self.detail.set_ocr_busy(False)
            if image_cleanup.expanded_search_finished:
                self.expanded_search_button.setEnabled(True)
                self.expanded_search_button.setText("扩大搜索")
            for restore in mutation_cleanup.delete_restores:
                self._restore_delete_view(
                    restore.item_id,
                    restore.was_selected,
                    restore.detail_was_visible,
                )
            if image_cleanup.pending or mutation_cleanup.pending:
                QTimer.singleShot(100, poll)

        QTimer.singleShot(0, poll)

    def _cancel_request(self, request: tuple[object, QObject] | None) -> None:
        if request is None:
            return
        token, signals = request
        self._cancel_async_token(token)

    def _cancel_item_requests(self, item_id: int) -> None:
        self.image_task_controller.cancel_item(item_id)

    def _cancel_background_requests(self) -> set[object]:
        self._cancel_library_refresh_request()
        self._cancel_item_search_request()
        self._cancel_item_page_request()
        cancelled_tokens = self.image_task_controller.cancel_for_shutdown()
        cancelled_tokens.update(self.mutation_controller.cancel_for_shutdown())
        bulk_token = self.bulk_image_controller.cancel()
        if bulk_token is not None:
            cancelled_tokens.add(bulk_token)
        return cancelled_tokens

    def _clear_background_request_state(self) -> None:
        self.image_task_controller.clear_state()
        self.mutation_controller.clear_background_state()
        self.expanded_search_button.setEnabled(True)
        self.expanded_search_button.setText("扩大搜索")

    def _cancel_and_wait_request(
        self,
        request: tuple[object, QObject] | None,
        timeout: float,
        *,
        process_events: bool = True,
    ) -> bool:
        if request is None:
            return True
        token, _signals = request
        app = QApplication.instance()
        pump_events = app.processEvents if process_events and app is not None else None
        return self._task_supervisor.wait_for_token(
            token,
            timeout,
            pump_events=pump_events,
        )

    def _cancel_and_wait_for_async_tasks(
        self,
        timeout: float = 1.5,
        *,
        require_bounded: bool = True,
        process_events: bool = True,
    ) -> bool:
        app = QApplication.instance()
        pump_events = app.processEvents if process_events and app is not None else None
        return self._task_supervisor.cancel_all_and_wait(
            timeout,
            require_bounded=require_bounded,
            pump_events=pump_events,
        )

    def generate_ai_description(self, item_id: int, *, automatic: bool = False) -> bool:
        return self._start_image_ai_operation(item_id, automatic, operation="ai")

    def generate_ocr(self, item_id: int, *, automatic: bool = False) -> bool:
        return self._start_image_ai_operation(item_id, automatic, operation="ocr")

    def _start_image_ai_operation(
        self,
        item_id: int,
        automatic: bool,
        *,
        operation: str,
    ) -> bool:
        is_ai = operation == "ai"
        requests = self._ai_requests if is_ai else self._ocr_requests
        invalid_title = "AI 描述" if is_ai else "OCR"
        invalid_message = (
            "当前只支持为图片生成 AI 描述。"
            if is_ai
            else "当前只支持识别图片中的文字。"
        )
        missing_hash_title = "AI 描述失败" if is_ai else "OCR 识别失败"
        capacity_title = "AI 任务繁忙" if is_ai else "OCR 任务繁忙"
        automatic_capacity_prefix = (
            "自动生成描述暂时无法启动" if is_ai else "自动 OCR 暂时无法启动"
        )

        item = self.database.get_item(item_id)
        if not item or item["kind"] != "image" or not item["path"]:
            if not automatic:
                QMessageBox.information(self, invalid_title, invalid_message)
            return False
        service = self._ai_service()
        if not service.configured:
            if not automatic:
                QMessageBox.information(
                    self,
                    "AI 服务未配置",
                    (
                        "请先在设置中填写 OpenAI-compatible 服务地址和视觉模型；"
                        "需要鉴权的服务还应填写 API Key。"
                        if is_ai
                        else "云端 OCR 需要先在设置中填写 OpenAI-compatible 服务地址和视觉模型；"
                        "需要鉴权的服务还应填写 API Key。"
                    ),
                )
                self.open_settings()
            return False
        expected_content_hash = item["content_hash"]
        if not expected_content_hash:
            if not automatic:
                QMessageBox.warning(
                    self,
                    missing_hash_title,
                    "图片索引缺少内容校验值，请重新导入后再试。",
                )
            return False
        if item_id in requests:
            if automatic:
                return False
            self.image_task_controller.cancel_operation(item_id, operation)
        if self.current_item_id == item_id:
            if is_ai:
                self.detail.set_ai_busy(True)
            else:
                self.detail.set_ocr_busy(True)
        try:
            self.image_task_controller.start_image_operation(
                item,
                service,
                operation=operation,
                automatic=automatic,
                estimated_bytes=self._image_task_estimate(item),
            )
        except (TaskCapacityExceeded, RuntimeError) as exc:
            if self.current_item_id == item_id:
                if is_ai:
                    self.detail.set_ai_busy(False, failed=True)
                else:
                    self.detail.set_ocr_busy(False, failed=True)
            if automatic:
                self.show_error_status(f"{automatic_capacity_prefix}：{exc}")
            else:
                QMessageBox.warning(self, capacity_title, str(exc))
            return False
        return True

    def _ocr_succeeded(
        self,
        token: object,
        signals: AsyncSignals,
        item_id: int,
        text: str,
        expected_content_hash: str,
    ) -> None:
        if not self.image_task_controller.is_current_operation(
            item_id,
            "ocr",
            token,
            signals,
        ):
            self._finish_async_token(token)
            return
        if self._closing or self._quit_in_progress:
            self.image_task_controller.finish_operation(
                item_id,
                "ocr",
                token,
                signals,
            )
            return
        try:
            saved = self.database.update_ocr_if_current(
                item_id, expected_content_hash, text
            )
        except Exception as exc:
            self._ocr_failed(token, signals, item_id, f"OCR 结果无法保存：{exc}")
            return
        if not saved:
            self.image_task_controller.finish_operation(
                item_id,
                "ocr",
                token,
                signals,
            )
            if self.current_item_id == item_id:
                self.detail.set_ocr_busy(False)
            self.show_status("图片已变化，已丢弃过期的 OCR 结果")
            return
        self.image_task_controller.finish_operation(
            item_id,
            "ocr",
            token,
            signals,
        )
        self._refresh_after_mutation()
        if self.current_item_id == item_id:
            self.update_detail(item_id)
        self.show_status("OCR 识别完成" if text else "图片中未识别到文字")

    def _ocr_failed(self, token: object, signals: AsyncSignals, item_id: int, message: str) -> None:
        matched, automatic = self.image_task_controller.finish_operation(
            item_id,
            "ocr",
            token,
            signals,
        )
        if not matched:
            return
        if self._closing or self._quit_in_progress:
            return
        if self.current_item_id == item_id:
            self.detail.set_ocr_busy(False, failed=True)
        if automatic:
            self.show_error_status(f"自动 OCR 失败：{message}")
        else:
            QMessageBox.warning(self, "OCR 识别失败", message)

    def expand_search(self) -> None:
        query = self.search.text().strip()
        if not query:
            QMessageBox.information(self, "扩大搜索", "先输入要扩大查找范围的搜索词。")
            return
        service = self._ai_service()
        if not service.configured:
            QMessageBox.information(
                self,
                "AI 服务未配置",
                "请先在设置中填写兼容服务地址和视觉模型名称。",
            )
            self.open_settings()
            return
        self.search_timer.stop()
        self._cancel_item_search_request()
        self._cancel_expanded_search_request()
        self.expanded_search_button.setEnabled(False)
        self.expanded_search_button.setText("扩展中…")
        try:
            self.image_task_controller.start_expanded_search(query, service)
        except (TaskCapacityExceeded, RuntimeError) as exc:
            self.expanded_search_button.setEnabled(True)
            self.expanded_search_button.setText("扩大搜索")
            QMessageBox.warning(self, "扩大搜索暂时不可用", str(exc))

    def _expanded_search_succeeded(
        self,
        token: object,
        signals: AsyncSignals,
        query: str,
        terms: object,
    ) -> None:
        if not self.image_task_controller.finish_expanded_search(token, signals):
            return
        if self._closing:
            return
        self.expanded_search_button.setEnabled(True)
        self.expanded_search_button.setText("扩大搜索")
        if self.search.text().strip() != query:
            return
        if not isinstance(terms, list) or not terms:
            QMessageBox.warning(self, "扩大搜索失败", "AI 服务没有返回可用的扩展搜索词。")
            return
        self._expanded_search_query = query
        self._expanded_search_terms = tuple(terms)
        self._refresh_search_items_async()
        self.show_status(f"搜索范围已扩大：使用 {len(self._expanded_search_terms):,} 个搜索词")

    def _expanded_search_failed(self, token: object, signals: AsyncSignals, message: str) -> None:
        if not self.image_task_controller.finish_expanded_search(token, signals):
            return
        if self._closing:
            return
        self.expanded_search_button.setEnabled(True)
        self.expanded_search_button.setText("扩大搜索")
        QMessageBox.warning(self, "扩大搜索失败", message)

    def _cancel_expanded_search_request(self) -> None:
        if self._expanded_search_request is None:
            return
        self.image_task_controller.cancel_expanded_search()
        if hasattr(self, "expanded_search_button"):
            self.expanded_search_button.setEnabled(True)
            self.expanded_search_button.setText("扩大搜索")

    def _ai_succeeded(
        self,
        token: object,
        signals: AsyncSignals,
        item_id: int,
        description: str,
        expected_content_hash: str,
    ) -> None:
        if not self.image_task_controller.is_current_operation(
            item_id,
            "ai",
            token,
            signals,
        ):
            self._finish_async_token(token)
            return
        if self._closing or self._quit_in_progress:
            self.image_task_controller.finish_operation(
                item_id,
                "ai",
                token,
                signals,
            )
            return
        try:
            saved = self.database.update_ai_if_current(
                item_id,
                expected_content_hash,
                description,
            )
        except Exception as exc:
            self._ai_failed(token, signals, item_id, f"AI 结果无法保存：{exc}")
            return
        if not saved:
            self.image_task_controller.finish_operation(
                item_id,
                "ai",
                token,
                signals,
            )
            if self.current_item_id == item_id:
                self.detail.set_ai_busy(False)
            self.show_status("图片已变化，已丢弃过期的 AI 结果")
            return
        self.image_task_controller.finish_operation(
            item_id,
            "ai",
            token,
            signals,
        )
        self._refresh_after_mutation()
        if self.current_item_id == item_id:
            self.update_detail(item_id)
        self.show_status("AI 描述已生成")

    def _ai_failed(self, token: object, signals: AsyncSignals, item_id: int, message: str) -> None:
        matched, automatic = self.image_task_controller.finish_operation(
            item_id,
            "ai",
            token,
            signals,
        )
        if not matched:
            return
        if self._closing or self._quit_in_progress:
            return
        if self.current_item_id == item_id:
            self.detail.set_ai_busy(False, failed=True)
        if automatic:
            self.show_error_status(f"自动生成描述失败：{message}")
        else:
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
        if is_windows_qt_platform():
            QTimer.singleShot(0, self._sync_windows_backdrop_window)

    def showEvent(self, event) -> None:
        self._ensure_native_resize_frame()
        self._ensure_power_saving_notification()
        super().showEvent(event)
        if is_windows_qt_platform():
            QTimer.singleShot(0, self._activate_windows_backdrop_after_show)
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

    def _activate_windows_backdrop_after_show(self) -> None:
        if self._closing or self._quit_in_progress:
            return
        self._apply_native_backdrop()
        self._sync_windows_backdrop_window()

    def hideEvent(self, event) -> None:
        if is_windows_qt_platform():
            self._hide_windows_backdrop_window()
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
        self._flush_pending_sidebar_collapsed_setting(force=True)
        if self._closing:
            return True
        if self._quit_in_progress or timeout <= 0:
            return False
        deadline = time.monotonic() + timeout
        cancelled_request_tokens: set[object] = set()

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        def abort(monitoring_was_active: bool) -> bool:
            self.shutdown_coordinator.abort_session_end(monitoring_was_active)
            self.backup_timer.start()
            self._quit_in_progress = False
            self.force_quit = False
            self._set_interactions_enabled(True)
            current_id = self.current_item_id
            self.detail.set_ai_busy(bool(current_id and current_id in self._ai_requests))
            self.detail.set_ocr_busy(bool(current_id and current_id in self._ocr_requests))
            if self._expanded_search_request is None:
                self.expanded_search_button.setEnabled(True)
                self.expanded_search_button.setText("扩大搜索")
            self._schedule_cancelled_request_cleanup(cancelled_request_tokens)
            return False

        self._quit_in_progress = True
        self.force_quit = True
        self._set_interactions_enabled(False)
        self.search_timer.stop()
        self._cancel_item_search_request()
        monitoring_was_active = self.shutdown_coordinator.prepare_session_end()

        if not self.shutdown_coordinator.persist_session_notes(
            remaining(),
            start_task=lambda token, target: self._start_async_task(token, target),
            cancel_task=lambda token: self._cancel_async_token(token),
            schedule_later=lambda delay_ms, callback: QTimer.singleShot(
                delay_ms,
                callback,
            ),
            is_closing=lambda: self._closing,
        ):
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
        cancelled_request_tokens.update(self._cancel_background_requests())
        if not self._cancel_and_wait_for_async_tasks(
            remaining(), require_bounded=True, process_events=False
        ):
            return abort(monitoring_was_active)
        self._clear_background_request_state()

        if not self.shutdown_coordinator.finish_session_end(remaining()):
            return abort(monitoring_was_active)

        return self._finalize_shutdown(
            executor_timeout=min(remaining(), 0.1),
            thumbnail_timeout_ms=max(0, int(remaining() * 1000)),
        )

    def _finalize_shutdown(
        self,
        *,
        executor_timeout: float,
        thumbnail_timeout_ms: int | None = None,
    ) -> bool:
        self._closing = True
        self.shutdown_coordinator.finalize_core(
            executor_timeout=executor_timeout,
            thumbnail_timeout_ms=thumbnail_timeout_ms,
        )
        self.tray.hide()
        application = QApplication.instance()
        if application is not None:
            application.exit(0)
        return True

    def quit_application(self) -> bool:
        self._flush_pending_sidebar_collapsed_setting(force=True)
        if self._closing or self._quit_in_progress:
            return False
        self._quit_in_progress = True
        self.force_quit = True
        self._set_interactions_enabled(False)
        self.search_timer.stop()
        self._cancel_item_search_request()
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
        cancelled_request_tokens = self._cancel_background_requests()
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
        self._clear_background_request_state()
        shutdown_result = self.shutdown_coordinator.shutdown_interactive_resources()
        if not shutdown_result.succeeded:
            self.backup_timer.start()
            self._quit_in_progress = False
            self.force_quit = False
            self._set_interactions_enabled(True)
            if self.current_item_id:
                self.update_detail(self.current_item_id)
            if shutdown_result.failure is ShutdownFailure.THUMBNAILS:
                title = "ClipSave 正在结束缩略图任务"
                message = "图片预览任务尚未结束，ClipSave 已取消退出。请稍后再次退出。"
            elif shutdown_result.failure is ShutdownFailure.BACKUP:
                title = "数据库备份失败"
                message = (
                    "退出前无法创建最新数据库备份，ClipSave 已取消退出。"
                    f"\n\n{shutdown_result.error}"
                )
            else:
                title = "ClipSave 正在保存"
                message = "仍有剪贴板内容正在写入本地磁盘。为避免数据丢失，ClipSave 暂时不会退出。请稍后再次退出。"
            QMessageBox.warning(self, title, message)
            return False
        return self._finalize_shutdown(executor_timeout=2.0)

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
