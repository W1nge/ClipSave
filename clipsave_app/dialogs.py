from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .markdown_view import SafeMarkdownBrowser, set_markdown_content
from .ui_primitives import (
    AutoHideScrollBar,
    DialogTitleBar,
    FluentComboBox,
    ThemedLineEdit,
    ThemedSelectableLabel,
    ToggleSwitch,
    fit_dialog_size,
    friendly_day,
    lucide_icon,
)


def _startfile_or_warn(parent: QWidget, path: Path | str) -> None:
    try:
        os.startfile(str(path))
    except OSError as exc:
        QMessageBox.warning(parent, "Open failed", f"Could not open the requested location.\n\n{exc}")


class MarkdownDialog(QDialog):
    def __init__(self, title: str, content: str, path: str | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setObjectName("FluentDialog")
        self.setProperty("markdownDialog", True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.resize(fit_dialog_size(self, QSize(920, 700)))

        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)
        title_bar = DialogTitleBar(title)
        title_bar.close_button.clicked.connect(self.reject)
        root.addWidget(title_bar)

        content_widget = QWidget()
        content_widget.setObjectName("DialogContent")
        layout = QVBoxLayout(content_widget)
        layout.setContentsMargins(18, 14, 18, 18)
        layout.setSpacing(10)
        if path:
            top = QHBoxLayout()
            top.addStretch(1)
            locate = QPushButton("在资源管理器中显示")
            locate.setIcon(lucide_icon("folder"))
            locate.clicked.connect(lambda: _startfile_or_warn(self, Path(path).parent))
            top.addWidget(locate)
            external = QPushButton("外部打开")
            external.setIcon(lucide_icon("external-link"))
            external.clicked.connect(lambda: _startfile_or_warn(self, path))
            top.addWidget(external)
            layout.addLayout(top)
        browser = SafeMarkdownBrowser()
        browser.setObjectName("MarkdownBrowser")
        browser.setOpenExternalLinks(False)
        set_markdown_content(browser, content)
        self.browser = browser
        layout.addWidget(browser)
        root.addWidget(content_widget, 1)


class TextDialog(QDialog):
    def __init__(self, title: str, content: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setObjectName("FluentDialog")
        self.setProperty("textDialog", True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.resize(fit_dialog_size(self, QSize(920, 700)))

        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)
        title_bar = DialogTitleBar(title)
        title_bar.close_button.clicked.connect(self.reject)
        root.addWidget(title_bar)

        content_widget = QWidget()
        content_widget.setObjectName("DialogContent")
        layout = QVBoxLayout(content_widget)
        layout.setContentsMargins(18, 14, 18, 18)
        browser = SafeMarkdownBrowser()
        browser.setObjectName("MarkdownBrowser")
        browser.setOpenExternalLinks(False)
        browser.setPlainText(content)
        self.browser = browser
        layout.addWidget(browser)
        root.addWidget(content_widget, 1)


class DateDialog(QDialog):
    day_selected = Signal(str)

    def __init__(self, days, parent=None):
        super().__init__(parent)
        self.setWindowTitle("按日期打开")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setObjectName("FluentDialog")
        self.setProperty("dateDialog", True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedSize(fit_dialog_size(self, QSize(420, 560)))
        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)
        title_bar = DialogTitleBar("按日期打开")
        title_bar.close_button.clicked.connect(self.reject)
        root.addWidget(title_bar)

        content = QWidget()
        content.setObjectName("DialogContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(22, 18, 22, 22)
        layout.setSpacing(12)
        description = QLabel("选择一天，查看当天保存的全部内容")
        description.setObjectName("Muted")
        layout.addWidget(description)
        list_widget = QListWidget()
        list_widget.setObjectName("DateList")
        list_widget.setVerticalScrollBar(AutoHideScrollBar())
        list_widget.setSpacing(2)
        for day, amount in days:
            item = QListWidgetItem(f"{friendly_day(day)}    {amount} 项")
            item.setData(Qt.ItemDataRole.UserRole, day)
            item.setSizeHint(QSize(0, 44))
            list_widget.addItem(item)
        list_widget.itemActivated.connect(lambda item: self._choose(item.data(Qt.ItemDataRole.UserRole)))
        list_widget.itemClicked.connect(lambda item: self._choose(item.data(Qt.ItemDataRole.UserRole)))
        layout.addWidget(list_widget)
        root.addWidget(content, 1)

    def _choose(self, day: str) -> None:
        self.day_selected.emit(day)
        self.accept()


def _settings_section_header(title: str, glyph: str) -> QWidget:
    header = QWidget()
    header.setObjectName("SettingsSectionHeader")
    layout = QHBoxLayout(header)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    icon = QLabel()
    icon.setObjectName("SettingsSectionIcon")
    icon.setFixedSize(20, 20)
    icon.setPixmap(lucide_icon(glyph, "#21a8fb", 17).pixmap(17, 17))
    layout.addWidget(icon)
    heading = QLabel(title)
    heading.setObjectName("SectionTitle")
    layout.addWidget(heading)
    divider = QFrame()
    divider.setObjectName("SettingsDivider")
    divider.setFixedHeight(1)
    layout.addWidget(divider, 1)
    return header


def _settings_field(label: str, field: QWidget) -> QWidget:
    container = QWidget()
    container.setObjectName("SettingsField")
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    caption = QLabel(label)
    caption.setObjectName("SettingsCaption")
    layout.addWidget(caption)
    field.setMinimumHeight(34)
    layout.addWidget(field)
    return container


class SettingsDialog(QDialog):
    import_requested = Signal()
    bulk_processing_requested = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.bulk_processing_confirmed = False
        self.setWindowTitle("ClipSave 设置")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setObjectName("FluentDialog")
        self.setProperty("settingsDialog", True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        preferred_size = QSize(720, 600)
        dialog_size = fit_dialog_size(self, preferred_size)
        self.setFixedSize(dialog_size)
        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)
        title_bar = DialogTitleBar("设置")
        title_bar.close_button.clicked.connect(self.reject)
        root.addWidget(title_bar)

        content = QWidget()
        content.setObjectName("DialogContent")
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(24, 14, 24, 14)
        content_layout.setSpacing(22)

        left_column = QWidget()
        left_column.setObjectName("SettingsColumn")
        left_layout = QVBoxLayout(left_column)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        left_layout.addWidget(_settings_section_header("常规", "sliders-horizontal"))
        self.close_to_tray = FluentComboBox()
        self.close_to_tray.addItem("关闭窗口时最小化到托盘", True)
        self.close_to_tray.addItem("关闭窗口时退出", False)
        self.close_to_tray.setCurrentIndex(0 if settings.get("close_to_tray", True) else 1)
        self.close_to_tray.setMinimumHeight(34)
        left_layout.addWidget(self.close_to_tray)
        self.start_with_windows = ToggleSwitch("开机时自动启动 ClipSave")
        self.start_with_windows.setChecked(settings.get("start_with_windows", False))
        self.start_with_windows.setToolTip("登录 Windows 后自动启动 ClipSave")
        left_layout.addWidget(self.start_with_windows)
        self.follow_system_theme = ToggleSwitch("跟随 Windows 深浅色主题")
        self.follow_system_theme.setChecked(settings.get("follow_system_theme", True))
        left_layout.addWidget(self.follow_system_theme)
        self.dark_theme_switch = ToggleSwitch("使用深色主题")
        self.dark_theme_switch.setChecked(settings.get("theme_mode", "light") == "dark")
        self.dark_theme_switch.setEnabled(not self.follow_system_theme.isChecked())
        self.follow_system_theme.toggled.connect(
            lambda checked: self.dark_theme_switch.setEnabled(not checked)
        )
        left_layout.addWidget(self.dark_theme_switch)
        hotkey_state = getattr(parent, "global_hotkey_registered", None)
        if hotkey_state is False:
            hotkey_text = "全局唤醒快捷键：Ctrl + Alt + V（注册失败，可能已被占用）"
        elif hotkey_state is True:
            hotkey_text = "全局唤醒快捷键：Ctrl + Alt + V（已启用）"
        else:
            hotkey_text = "全局唤醒快捷键：Ctrl + Alt + V"
        self.hotkey_status = QLabel(hotkey_text)
        self.hotkey_status.setObjectName("SettingsCaption")
        self.hotkey_status.setWordWrap(True)
        left_layout.addWidget(self.hotkey_status)
        left_layout.addSpacing(8)
        from .constants import DATA_DIR, LIBRARY_DIR

        left_layout.addWidget(_settings_section_header("本地存储", "hard-drive"))
        for caption_text, path, attribute_name in (
            ("剪贴板文件", LIBRARY_DIR, "library_path_label"),
            ("数据库和设置", DATA_DIR, "data_path_label"),
        ):
            caption = QLabel(caption_text)
            caption.setObjectName("SettingsCaption")
            left_layout.addWidget(caption)
            path_label = ThemedSelectableLabel(str(path))
            path_label.setObjectName("SettingsPath")
            path_label.setWordWrap(False)
            path_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            path_label.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
            )
            path_label.setToolTip(str(path))
            setattr(self, attribute_name, path_label)
            left_layout.addWidget(path_label)
        storage_privacy = QLabel("所有自动捕获内容仅写入本机。")
        storage_privacy.setObjectName("Muted")
        left_layout.addWidget(storage_privacy)
        storage_actions = QHBoxLayout()
        storage_actions.setSpacing(8)
        self.import_button = QPushButton("导入文件")
        self.import_button.setObjectName("SettingsAction")
        self.import_button.setIcon(lucide_icon("plus"))
        self.import_button.clicked.connect(self.import_requested.emit)
        storage_actions.addWidget(self.import_button, 1)
        open_storage = QPushButton("打开本地资料库")
        open_storage.setObjectName("SettingsAction")
        open_storage.setIcon(lucide_icon("folder"))
        open_storage.clicked.connect(lambda: _startfile_or_warn(self, LIBRARY_DIR))
        self.open_storage_button = open_storage
        storage_actions.addWidget(open_storage, 1)
        left_layout.addLayout(storage_actions)
        left_layout.addStretch(1)

        right_column = QWidget()
        right_column.setObjectName("SettingsColumn")
        right_layout = QVBoxLayout(right_column)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)
        right_layout.addWidget(_settings_section_header("AI 服务", "sparkles"))
        ai_caption = QLabel("OpenAI-compatible 视觉模型")
        ai_caption.setObjectName("SettingsCaption")
        right_layout.addWidget(ai_caption)
        self.base_url = ThemedLineEdit(settings.get("ai_base_url", ""))
        self.base_url.setPlaceholderText("Base URL，例如 https://example.com/v1")
        right_layout.addWidget(_settings_field("Base URL", self.base_url))
        self.api_key = ThemedLineEdit(settings.get("ai_api_key", ""))
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("API Key（仅保存在本机）")
        right_layout.addWidget(_settings_field("API Key", self.api_key))
        self.vision_model = ThemedLineEdit(settings.get("ai_vision_model", ""))
        self.vision_model.setPlaceholderText("视觉模型名称")
        right_layout.addWidget(_settings_field("视觉模型", self.vision_model))
        right_layout.addSpacing(2)
        self.auto_ocr = ToggleSwitch("图片自动 OCR")
        self.auto_ocr.setChecked(settings.get("auto_ocr", False))
        right_layout.addWidget(self.auto_ocr)
        self.auto_description = ToggleSwitch("图片自动生成描述")
        self.auto_description.setChecked(settings.get("auto_description", False))
        right_layout.addWidget(self.auto_description)
        privacy = QLabel("自动捕获不会联网；自动 OCR/描述会将新图片发送到上方视觉模型。")
        privacy.setObjectName("Muted")
        privacy.setWordWrap(True)
        right_layout.addWidget(privacy)
        self.bulk_image_button = QPushButton("一键 OCR 并生成全部图片描述")
        self.bulk_image_button.setObjectName("SettingsAction")
        self.bulk_image_button.setIcon(lucide_icon("scan-text"))
        self.bulk_image_button.clicked.connect(self.bulk_processing_requested.emit)
        right_layout.addWidget(self.bulk_image_button)
        self.bulk_progress_label = QLabel()
        self.bulk_progress_label.setObjectName("SettingsCaption")
        self.bulk_progress_label.setWordWrap(False)
        right_layout.addWidget(self.bulk_progress_label)
        self.bulk_progress = QProgressBar()
        self.bulk_progress.setObjectName("BulkImageProgress")
        self.bulk_progress.setTextVisible(False)
        right_layout.addWidget(self.bulk_progress)
        self._bulk_progress_provider = parent
        self._bulk_progress_timer = QTimer(self)
        self._bulk_progress_timer.setInterval(250)
        self._bulk_progress_timer.timeout.connect(self._refresh_bulk_progress)
        self._bulk_progress_timer.start()
        self._refresh_bulk_progress()
        right_layout.addStretch(1)

        compact = dialog_size != preferred_size
        if compact:
            content_layout.setDirection(QVBoxLayout.Direction.TopToBottom)
            content_layout.setSpacing(18)
            content_layout.addWidget(left_column)
            content_layout.addWidget(right_column)
        else:
            content_layout.addWidget(left_column, 1)
            column_divider = QFrame()
            column_divider.setObjectName("SettingsColumnDivider")
            column_divider.setFixedWidth(1)
            content_layout.addWidget(column_divider)
            content_layout.addWidget(right_column, 1)

        if dialog_size != preferred_size:
            scroll = QScrollArea()
            scroll.setObjectName("DialogScroll")
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setVerticalScrollBar(AutoHideScrollBar())
            scroll.setWidget(content)
            root.addWidget(scroll, 1)
        else:
            root.addWidget(content, 1)

        footer = QFrame()
        footer.setObjectName("DialogFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(16, 10, 16, 12)
        footer_layout.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        footer_layout.addWidget(cancel)
        save = QPushButton("保存")
        save.setObjectName("Primary")
        save.clicked.connect(self.accept)
        footer_layout.addWidget(save)
        root.addWidget(footer)

    def _refresh_bulk_progress(self) -> None:
        provider = getattr(self._bulk_progress_provider, "bulk_image_progress_snapshot", None)
        if callable(provider):
            state = provider()
        else:
            state = {
                "active": False,
                "resumable": False,
                "processed": 0,
                "total": 0,
                "phase": "",
                "error": "",
            }
        active = bool(state.get("active"))
        resumable = bool(state.get("resumable"))
        processed = max(0, int(state.get("processed") or 0))
        total = max(0, int(state.get("total") or 0))
        phase = str(state.get("phase") or "")
        error = str(state.get("error") or "")
        visible = active or resumable or bool(error)
        self.bulk_progress_label.setVisible(visible)
        self.bulk_progress.setVisible(visible and total > 0)
        if total > 0:
            self.bulk_progress.setRange(0, total)
            self.bulk_progress.setValue(min(processed, total))
            self.bulk_progress.setFormat(f"{processed:,} / {total:,}")
        if active:
            self.bulk_image_button.setText("批量处理中…")
            self.bulk_image_button.setEnabled(False)
        elif resumable:
            self.bulk_image_button.setText("继续 OCR 与描述")
            self.bulk_image_button.setEnabled(True)
        else:
            self.bulk_image_button.setText("一键 OCR 并生成全部图片描述")
            self.bulk_image_button.setEnabled(True)
        if error:
            self.bulk_progress_label.setText(
                f"{phase or '已暂停'}  {processed:,}/{total:,}：{error[:80]}"
            )
        elif visible:
            self.bulk_progress_label.setText(
                f"{phase or '等待继续'}  {processed:,}/{total:,}"
            )
        else:
            self.bulk_progress_label.clear()

    def persist_bulk_configuration(self) -> bool:
        try:
            self.settings.update(
                {
                    "ai_base_url": self.base_url.text().strip(),
                    "ai_api_key": self.api_key.text().strip(),
                    "ai_vision_model": self.vision_model.text().strip(),
                }
            )
        except (OSError, TypeError, ValueError) as exc:
            QMessageBox.warning(self, "AI 设置保存失败", str(exc))
            return False
        return True

    def accept(self) -> None:
        values = {
            "close_to_tray": self.close_to_tray.currentData(),
            "start_with_windows": self.start_with_windows.isChecked(),
            "follow_system_theme": self.follow_system_theme.isChecked(),
            "theme_mode": "dark" if self.dark_theme_switch.isChecked() else "light",
            "ai_base_url": self.base_url.text().strip(),
            "ai_api_key": self.api_key.text().strip(),
            "ai_vision_model": self.vision_model.text().strip(),
            "auto_ocr": self.auto_ocr.isChecked(),
            "auto_description": self.auto_description.isChecked(),
        }
        if (values["auto_ocr"] or values["auto_description"]) and not (
            values["ai_base_url"] and values["ai_vision_model"]
        ):
            QMessageBox.warning(
                self,
                "AI 服务未配置",
                "启用自动 OCR 或自动描述前，请先填写 Base URL 和视觉模型名称。",
            )
            return
        try:
            self.settings.update(values)
        except (OSError, TypeError, ValueError) as exc:
            QMessageBox.warning(self, "设置保存失败", str(exc))
            return
        super().accept()
