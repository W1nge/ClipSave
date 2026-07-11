from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)
from send2trash import send2trash

from .constants import APP_NAME
from .database import LibraryDatabase
from .services import AIService, ClipboardService, apply_windows_acrylic
from .ocr_service import WindowsOCRService
from .settings import Settings
from .storage import is_under_local_store
from .styles import LIGHT_STYLESHEET
from .widgets import (
    AssetGrid,
    AssetTable,
    DateDialog,
    DetailPanel,
    IconButton,
    MarkdownDialog,
    SettingsDialog,
    Sidebar,
    lucide_icon,
)


class AsyncSignals(QObject):
    succeeded = Signal(int, str, object)
    failed = Signal(str)


class MainWindow(QMainWindow):
    def __init__(self, database: LibraryDatabase, settings: Settings, app_icon: QIcon, scan_on_start: bool = True):
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
        self.force_quit = False

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon)
        self.resize(1440, 880)
        self.setMinimumSize(980, 640)
        self.setStyleSheet(LIGHT_STYLESHEET)
        self.build_ui()
        self.build_tray()
        self.build_shortcuts()

        self.clipboard_service = ClipboardService(database, self)
        self.clipboard_service.captured.connect(self.on_captured)
        self.clipboard_service.failed.connect(self.show_error_status)
        self.clipboard_service.state_changed.connect(self.update_monitor_button)
        if settings.get("monitoring", True):
            self.clipboard_service.start()

        self.database.mark_missing_files()
        imported = self.database.scan_legacy_files() if scan_on_start else 0
        self.refresh_library()
        if imported:
            self.show_status(f"已导入 {imported} 个现有文件")
        QTimer.singleShot(100, lambda: apply_windows_acrylic(self))

    def build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("AppRoot")
        self.setCentralWidget(root)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.sidebar = Sidebar()
        self.sidebar.navigation_requested.connect(self.navigate)
        self.sidebar.add_collection_requested.connect(self.add_collection)
        self.sidebar.add_tag_requested.connect(self.add_global_tag)
        self.sidebar.settings_requested.connect(self.open_settings)
        self.sidebar.collapsed_changed.connect(lambda value: self.settings.set("sidebar_collapsed", value))
        root_layout.addWidget(self.sidebar)

        middle = QWidget()
        middle.setObjectName("ContentSurface")
        middle_layout = QVBoxLayout(middle)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        middle_layout.setSpacing(0)
        root_layout.addWidget(middle, 1)

        top_bar = QFrame()
        top_bar.setObjectName("TopBar")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(16, 10, 16, 10)
        top_layout.setSpacing(8)
        import_button = QPushButton("导入")
        import_button.setIcon(lucide_icon("plus"))
        import_button.clicked.connect(self.import_files)
        top_layout.addWidget(import_button)
        top_layout.addStretch(1)
        self.search = __import__("PySide6.QtWidgets", fromlist=["QLineEdit"]).QLineEdit()
        self.search.setPlaceholderText("搜索剪贴板内容、文件名、标签、OCR 或 AI 描述  (Ctrl+K)")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(560)
        self.search.setMinimumWidth(320)
        self.search.textChanged.connect(lambda _text: self.search_timer.start())
        top_layout.addWidget(self.search, 3)
        self.semantic_button = QPushButton("语义搜索")
        self.semantic_button.setIcon(lucide_icon("sparkles"))
        self.semantic_button.setToolTip("使用已生成的图片向量按含义搜索")
        self.semantic_button.clicked.connect(self.semantic_search)
        top_layout.addWidget(self.semantic_button)
        top_layout.addStretch(1)
        self.sort_button = QPushButton("按时间排序  ▾")
        self.sort_button.clicked.connect(self.open_sort_menu)
        top_layout.addWidget(self.sort_button)
        self.monitor_button = IconButton("pause", "暂停剪贴板监听")
        self.monitor_button.clicked.connect(self.toggle_monitor)
        top_layout.addWidget(self.monitor_button)
        self.grid_button = IconButton("grid", "网格视图")
        self.grid_button.clicked.connect(lambda: self.set_view_mode("grid"))
        top_layout.addWidget(self.grid_button)
        self.list_button = IconButton("list", "列表视图")
        self.list_button.clicked.connect(lambda: self.set_view_mode("list"))
        top_layout.addWidget(self.list_button)
        self.detail_button = IconButton("info", "显示详情")
        self.detail_button.clicked.connect(self.toggle_detail)
        top_layout.addWidget(self.detail_button)
        middle_layout.addWidget(top_bar)

        title_bar = QFrame()
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
        middle_layout.addWidget(title_bar)

        content_row = QHBoxLayout()
        content_row.setContentsMargins(0, 0, 0, 0)
        content_row.setSpacing(0)
        self.view_stack = QStackedWidget()
        self.grid = AssetGrid()
        self.grid.item_selected.connect(self.select_item)
        self.grid.item_activated.connect(self.activate_item)
        self.grid.favorite_requested.connect(self.set_favorite)
        self.table = AssetTable()
        self.table.item_selected.connect(self.select_item)
        self.table.item_activated.connect(self.activate_item)
        self.view_stack.addWidget(self.grid)
        self.view_stack.addWidget(self.table)
        content_row.addWidget(self.view_stack, 1)
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

        footer = QFrame()
        footer.setObjectName("TopBar")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(18, 6, 18, 6)
        self.status = QLabel("就绪")
        self.status.setObjectName("Muted")
        footer_layout.addWidget(self.status)
        footer_layout.addStretch()
        self.capture_state = QLabel("● 本地自动捕获：已开启")
        self.capture_state.setStyleSheet("color:#169c55;")
        footer_layout.addWidget(self.capture_state)
        middle_layout.addWidget(footer)

        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(220)
        self.search_timer.timeout.connect(self.refresh_items)
        self.set_view_mode(self.settings.get("view_mode", "grid"))
        self.sidebar.set_collapsed(bool(self.settings.get("sidebar_collapsed", False)), animate=False)

    def build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self.app_icon, self)
        self.tray.setToolTip("ClipSave - 正在监听剪贴板")
        menu = QMenu()
        show_action = menu.addAction("显示 ClipSave")
        show_action.triggered.connect(self.bring_to_front)
        self.tray_monitor_action = menu.addAction("暂停监听")
        self.tray_monitor_action.triggered.connect(self.toggle_monitor)
        menu.addSeparator()
        quit_action = menu.addAction("退出")
        quit_action.triggered.connect(self.quit_application)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.bring_to_front() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        self.tray.show()

    def build_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+K"), self, activated=self.focus_search)
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self.focus_search)
        QShortcut(QKeySequence("Ctrl+B"), self, activated=self.sidebar.toggle_collapsed)
        QShortcut(QKeySequence("Ctrl+I"), self, activated=self.toggle_detail)
        QShortcut(QKeySequence("Delete"), self, activated=lambda: self.current_item_id and self.delete_item(self.current_item_id))
        QShortcut(QKeySequence("Ctrl+C"), self, activated=lambda: self.current_item_id and self.copy_item(self.current_item_id))

    def refresh_library(self) -> None:
        counts = self.database.counts()
        self.sidebar.set_primary(counts)
        self.sidebar.set_collections(self.database.collections())
        self.sidebar.set_tags(self.database.tags())
        self.detail.set_collections(self.database.collections())
        self.refresh_items()

    def refresh_items(self) -> None:
        self.current_items = self.database.query_items(
            query=self.search.text().strip(),
            kind=self.current_kind,
            favorite=self.current_favorite,
            day=self.current_day,
            recent_days=7 if self.current_recent else None,
            collection_id=self.current_collection,
            tag_id=self.current_tag,
            sort=self.current_sort,
        )
        self.grid.set_items(self.current_items)
        self.table.set_items(self.current_items)
        self.result_count.setText(f"{len(self.current_items):,} 项")
        filters = []
        if self.current_day:
            filters.append(self.current_day)
        if self.search.text().strip():
            filters.append(f"搜索：{self.search.text().strip()}")
        self.filter_hint.setText("  ·  ".join(filters))

    def navigate(self, key: str, value) -> None:
        if key == "date":
            dialog = DateDialog(self.database.days(), self)
            dialog.day_selected.connect(self.open_day)
            dialog.exec()
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
        self.sidebar.set_active(key if key in self.sidebar.nav_buttons else "")
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
        self.current_item_id = item_id
        if self.detail.isVisible():
            self.update_detail(item_id)

    def update_detail(self, item_id: int) -> None:
        item = self.database.get_item(item_id)
        if item:
            self.detail.set_item(item)

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
        self.view_stack.setCurrentWidget(self.grid if mode == "grid" else self.table)
        self.settings.set("view_mode", mode)
        self.grid_button.setStyleSheet("background:rgba(47,125,246,28); color:#1769d2;" if mode == "grid" else "")
        self.list_button.setStyleSheet("background:rgba(47,125,246,28); color:#1769d2;" if mode == "list" else "")

    def open_sort_menu(self) -> None:
        menu = QMenu(self)
        entries = [("newest", "捕获时间：最新优先"), ("oldest", "捕获时间：最早优先"), ("name", "名称"), ("size", "文件大小"), ("type", "类型")]
        for key, label in entries:
            action = menu.addAction(("✓  " if key == self.current_sort else "    ") + label)
            action.triggered.connect(lambda _checked=False, value=key, text=label: self.set_sort(value, text))
        menu.exec(self.sort_button.mapToGlobal(self.sort_button.rect().bottomLeft()))

    def set_sort(self, key: str, label: str) -> None:
        self.current_sort = key
        self.settings.set("sort", key)
        self.sort_button.setText(label + "  ▾")
        self.refresh_items()

    def toggle_monitor(self) -> None:
        self.clipboard_service.toggle()
        self.settings.set("monitoring", self.clipboard_service.timer.isActive())

    def update_monitor_button(self, active: bool) -> None:
        self.monitor_button.setIcon(lucide_icon("pause" if active else "play"))
        self.monitor_button.setToolTip("暂停剪贴板监听" if active else "继续剪贴板监听")
        self.capture_state.setText("● 本地自动捕获：已开启" if active else "○ 本地自动捕获：已暂停")
        self.capture_state.setStyleSheet("color:#169c55;" if active else "color:#7b8798;")
        if hasattr(self, "tray_monitor_action"):
            self.tray_monitor_action.setText("暂停监听" if active else "继续监听")
            self.tray.setToolTip("ClipSave - " + ("正在监听剪贴板" if active else "监听已暂停"))

    def on_captured(self, _item_id: int) -> None:
        self.show_status("已保存一条新的剪贴板内容")
        self.refresh_library()

    def activate_item(self, item_id: int) -> None:
        item = self.database.get_item(item_id)
        if not item:
            return
        if item["kind"] == "markdown":
            MarkdownDialog(item["title"], item["content"], item["path"], self).exec()
        elif item["kind"] == "image" and item["path"]:
            os.startfile(item["path"])
        else:
            self.copy_item(item_id)

    def copy_item(self, item_id: int) -> None:
        item = self.database.get_item(item_id)
        if not item:
            return
        clipboard = QApplication.clipboard()
        if item["kind"] == "image" and item["path"]:
            image = QPixmap(item["path"]).toImage()
            self.clipboard_service.suppress_image(image)
            clipboard.setImage(image)
        else:
            text = item["content"]
            self.clipboard_service.suppress_text(text)
            clipboard.setText(text)
        self.show_status("已复制到剪贴板")

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
        if QMessageBox.question(self, "删除内容", message) != QMessageBox.StandardButton.Yes:
            return
        if managed_file and Path(item["path"]).exists():
            send2trash(item["path"])
        self.database.remove_item(item_id)
        self.current_item_id = None
        self.hide_detail()
        self.refresh_library()
        self.show_status("内容已移入回收站")

    def set_favorite(self, item_id: int, value: bool) -> None:
        self.database.set_favorite(item_id, value)
        self.refresh_library()
        if self.detail.isVisible():
            self.update_detail(item_id)

    def save_notes(self, item_id: int, notes: str) -> None:
        self.database.set_notes(item_id, notes)
        self.show_status("备注已保存")

    def add_collection(self) -> None:
        name, ok = QInputDialog.getText(self, "新建集合", "集合名称")
        if ok and name.strip():
            self.database.create_collection(name.strip())
            self.refresh_library()

    def add_global_tag(self) -> None:
        if not self.current_item_id:
            QMessageBox.information(self, "添加标签", "请先选择一项内容。")
            return
        self.add_tag_to_item(self.current_item_id)

    def add_tag_to_item(self, item_id: int) -> None:
        name, ok = QInputDialog.getText(self, "添加标签", "标签名称")
        if ok and name.strip():
            self.database.add_tag(item_id, name.strip())
            self.refresh_library()
            self.update_detail(item_id)

    def remove_tag_from_item(self, item_id: int, name: str) -> None:
        self.database.remove_tag(item_id, name)
        self.refresh_library()
        self.update_detail(item_id)

    def set_item_collection(self, item_id: int, collection_id) -> None:
        self.database.set_collection(item_id, collection_id)
        self.refresh_items()
        self.show_status("集合已更新")

    def import_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "导入内容", "", "支持的文件 (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.md)")
        added = 0
        for filename in paths:
            added += int(self.database.import_file(Path(filename), copy_to_library=True))
        if paths:
            self.refresh_library()
            self.show_status(f"已导入 {added} 项，跳过 {len(paths) - added} 项重复内容")

    def open_settings(self) -> None:
        SettingsDialog(self.settings, self).exec()

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
        self.detail.ai_button.setEnabled(False)
        self.detail.ai_button.setText("生成中…")
        self.ai_signals = AsyncSignals()
        self.ai_signals.succeeded.connect(self._ai_succeeded)
        self.ai_signals.failed.connect(self._ai_failed)

        def work() -> None:
            try:
                description = service.describe_image(Path(item["path"]))
                embedding = service.embed(description)
                self.ai_signals.succeeded.emit(item_id, description, embedding)
            except Exception as exc:
                self.ai_signals.failed.emit(str(exc))

        threading.Thread(target=work, daemon=True).start()

    def generate_ocr(self, item_id: int) -> None:
        item = self.database.get_item(item_id)
        if not item or item["kind"] != "image" or not item["path"]:
            QMessageBox.information(self, "OCR", "当前只支持识别图片中的文字。")
            return
        self.detail.ocr_button.setEnabled(False)
        self.detail.ocr_button.setText("识别中…")
        self.ocr_signals = AsyncSignals()
        self.ocr_signals.succeeded.connect(self._ocr_succeeded)
        self.ocr_signals.failed.connect(self._ocr_failed)

        def work() -> None:
            try:
                text = WindowsOCRService.recognize(Path(item["path"]))
                self.ocr_signals.succeeded.emit(item_id, text, None)
            except Exception as exc:
                self.ocr_signals.failed.emit(str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _ocr_succeeded(self, item_id: int, text: str, _unused) -> None:
        self.database.update_ocr(item_id, text)
        self.detail.ocr_button.setEnabled(True)
        self.detail.ocr_button.setText("重新识别")
        self.update_detail(item_id)
        self.show_status("OCR 识别完成" if text else "图片中未识别到文字")

    def _ocr_failed(self, message: str) -> None:
        self.detail.ocr_button.setEnabled(True)
        self.detail.ocr_button.setText("重试")
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
        self.semantic_button.setEnabled(False)
        self.semantic_button.setText("搜索中…")
        self.semantic_signals = AsyncSignals()
        self.semantic_signals.succeeded.connect(self._semantic_succeeded)
        self.semantic_signals.failed.connect(self._semantic_failed)

        def work() -> None:
            try:
                query_vector = service.embed(query)
                scored = []
                for row in embedded:
                    vector = json.loads(row["embedding"])
                    scored.append((service.similarity(query_vector, vector), row["id"]))
                scored.sort(reverse=True)
                self.semantic_signals.succeeded.emit(-1, "", [item_id for score, item_id in scored if score > 0])
            except Exception as exc:
                self.semantic_signals.failed.emit(str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _semantic_succeeded(self, _item_id: int, _text: str, ordered_ids) -> None:
        base_items = self.database.query_items(
            kind=self.current_kind,
            favorite=self.current_favorite,
            day=self.current_day,
            recent_days=7 if self.current_recent else None,
            collection_id=self.current_collection,
            tag_id=self.current_tag,
            sort=self.current_sort,
        )
        records = {row["id"]: row for row in base_items}
        self.current_items = [records[item_id] for item_id in ordered_ids if item_id in records]
        self.grid.set_items(self.current_items)
        self.table.set_items(self.current_items)
        self.result_count.setText(f"{len(self.current_items):,} 项 · 按语义相关度")
        self.semantic_button.setEnabled(True)
        self.semantic_button.setText("语义搜索")

    def _semantic_failed(self, message: str) -> None:
        self.semantic_button.setEnabled(True)
        self.semantic_button.setText("语义搜索")
        QMessageBox.warning(self, "语义搜索失败", message)

    def _ai_succeeded(self, item_id: int, description: str, embedding) -> None:
        self.database.update_ai(item_id, description, embedding)
        self.detail.ai_button.setEnabled(True)
        self.detail.ai_button.setText("重新生成")
        self.update_detail(item_id)
        self.refresh_items()
        self.show_status("AI 描述已生成")

    def _ai_failed(self, message: str) -> None:
        self.detail.ai_button.setEnabled(True)
        self.detail.ai_button.setText("重试")
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

    def show_status(self, text: str) -> None:
        self.status.setText(text)
        QTimer.singleShot(2800, lambda: self.status.setText("就绪"))

    def show_error_status(self, text: str) -> None:
        self.status.setText(f"剪贴板读取失败：{text[:80]}")

    def quit_application(self) -> None:
        self.force_quit = True
        self.clipboard_service.stop()
        self.database.close()
        self.tray.hide()
        QApplication.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.force_quit or not self.settings.get("close_to_tray", True):
            self.quit_application()
            event.accept()
        else:
            event.ignore()
            self.hide()
            self.tray.showMessage("ClipSave", "ClipSave 仍在后台保存剪贴板内容。", QSystemTrayIcon.MessageIcon.Information, 1800)
