from __future__ import annotations

from PySide6.QtCore import (
    QRect,
    QSize,
    Qt,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .ui_controls import AutoHideScrollBar, IconButton, ThemedSelectableLabel, lucide_icon


class _ManualWindowDrag:
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            window = self.window()
            host = getattr(window, "_composition_host", None)
            if host is not None and not host.closed:
                host.start_system_move()
                event.accept()
                return
            handle = window.windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()
                return
            is_maximized = getattr(window, "_window_is_maximized", window.isMaximized)()
            if not is_maximized and not window.isFullScreen():
                self._drag_offset = event.globalPosition().toPoint() - window.pos()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        offset = getattr(self, "_drag_offset", None)
        if offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.window().move(event.globalPosition().toPoint() - offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)


class WindowTitleBar(_ManualWindowDrag, QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("WindowTitleBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(32)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 0, 0)
        layout.setSpacing(0)
        layout.addStretch(1)

        self.minimize_button = self._window_button("minus", "最小化")
        self.maximize_button = self._window_button("square", "最大化")
        self.close_button = self._window_button("x", "关闭")
        self.close_button.setObjectName("CloseWindowButton")
        layout.addWidget(self.minimize_button)
        layout.addWidget(self.maximize_button)
        layout.addWidget(self.close_button)

    @staticmethod
    def _window_button(glyph: str, tooltip: str) -> IconButton:
        button = IconButton(glyph, tooltip)
        button.setObjectName("WindowButton")
        button.setFixedSize(46, 32)
        button.setIconSize(QSize(14, 14))
        return button

    def update_maximize_state(self, maximized: bool) -> None:
        self.maximize_button.set_glyph("copy" if maximized else "square")
        label = "还原" if maximized else "最大化"
        self.maximize_button.setToolTip(label)
        self.maximize_button.setAccessibleName(label)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            window = self.window()
            toggle = getattr(window, "toggle_maximized", None)
            if callable(toggle):
                toggle()
            else:
                window.showNormal() if window.isMaximized() else window.showMaximized()
                self.update_maximize_state(window.isMaximized())
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class DraggableBar(_ManualWindowDrag, QFrame):
    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            window = self.window()
            toggle = getattr(window, "toggle_maximized", None)
            if callable(toggle):
                toggle()
            else:
                window.showNormal() if window.isMaximized() else window.showMaximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


def _available_dialog_size(widget: QWidget, margin: int = 32) -> QSize:
    screen = widget.screen()
    if screen is None:
        screen = QApplication.primaryScreen()
    if screen is None:
        return QSize(1_000_000, 1_000_000)
    available = screen.availableGeometry()
    return QSize(max(1, available.width() - margin), max(1, available.height() - margin))


def _fit_dialog_size(widget: QWidget, preferred: QSize, margin: int = 32) -> QSize:
    available = _available_dialog_size(widget, margin)
    return QSize(min(preferred.width(), available.width()), min(preferred.height(), available.height()))


fit_dialog_size = _fit_dialog_size


class DialogTitleBar(DraggableBar):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("DialogTitleBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(46)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 0, 6, 0)
        heading = QLabel(title)
        heading.setObjectName("SectionTitle")
        heading.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(heading)
        layout.addStretch(1)
        self.close_button = IconButton("close", "关闭")
        layout.addWidget(self.close_button)


class FluentMessageDialog(QDialog):
    _ICONS = {
        "information": ("info", "#21a8fb"),
        "warning": ("triangle-alert", "#f5a623"),
        "critical": ("circle-x", "#d13438"),
        "question": ("circle-alert", "#21a8fb"),
    }

    def __init__(
        self,
        title: str,
        message: str,
        parent=None,
        *,
        kind: str = "information",
        accept_text: str = "确定",
        cancel_text: str | None = None,
        destructive: bool = False,
        default_accept: bool = True,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setObjectName("FluentDialog")
        self.setProperty("messageDialog", True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)
        title_bar = DialogTitleBar(title)
        title_bar.close_button.clicked.connect(self.reject)
        root.addWidget(title_bar)

        content = QWidget()
        content.setObjectName("DialogContent")
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(24, 20, 24, 20)
        content_layout.setSpacing(14)

        glyph, color = self._ICONS.get(kind, self._ICONS["information"])
        icon = QLabel()
        icon.setObjectName("MessageIcon")
        icon.setFixedSize(24, 24)
        icon.setPixmap(lucide_icon(glyph, color, 22).pixmap(22, 22))
        icon.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        content_layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        self.message_label = ThemedSelectableLabel(message)
        self.message_label.setObjectName("MessageText")
        self.message_label.setTextFormat(Qt.TextFormat.PlainText)
        self.message_label.setWordWrap(True)
        self.message_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.message_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        message_container = QWidget()
        message_layout = QVBoxLayout(message_container)
        message_layout.setContentsMargins(0, 0, 0, 0)
        message_layout.addWidget(self.message_label)
        message_layout.addStretch(1)

        dialog_width = _fit_dialog_size(self, QSize(480, 200)).width()
        message_width = min(360, max(80, dialog_width - 110))
        text_height = self.message_label.fontMetrics().boundingRect(
            QRect(0, 0, message_width, 10_000),
            Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
            message,
        ).height()
        message_height = max(56, min(260, text_height + 8))
        message_scroll = QScrollArea()
        message_scroll.setObjectName("MessageScroll")
        message_scroll.setFrameShape(QFrame.Shape.NoFrame)
        message_scroll.setWidgetResizable(True)
        message_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        message_scroll.setVerticalScrollBar(AutoHideScrollBar())
        message_scroll.setFixedHeight(message_height)
        message_scroll.setWidget(message_container)
        content_layout.addWidget(message_scroll, 1)
        root.addWidget(content, 1)

        footer = QFrame()
        footer.setObjectName("DialogFooter")
        footer.setFixedHeight(58)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(16, 10, 16, 12)
        footer_layout.addStretch(1)
        self.cancel_button = None
        if cancel_text is not None:
            self.cancel_button = QPushButton(cancel_text)
            self.cancel_button.clicked.connect(self.reject)
            footer_layout.addWidget(self.cancel_button)
        self.accept_button = QPushButton(accept_text)
        self.accept_button.setObjectName("Danger" if destructive else "Primary")
        self.accept_button.clicked.connect(self.accept)
        footer_layout.addWidget(self.accept_button)
        root.addWidget(footer)

        default_button = self.accept_button if default_accept else self.cancel_button
        if default_button is not None:
            default_button.setDefault(True)
            default_button.setFocus()
        preferred_size = QSize(480, 46 + 40 + message_height + 58 + 2)
        dialog_size = _fit_dialog_size(self, preferred_size)
        content_height = max(32, dialog_size.height() - 46 - 58 - 42)
        message_scroll.setFixedHeight(min(message_height, content_height))
        self.setFixedSize(dialog_size)


class FluentMessageBox:
    StandardButton = QMessageBox.StandardButton

    @staticmethod
    def _exec(dialog: FluentMessageDialog) -> int:
        try:
            return dialog.exec()
        finally:
            dialog.deleteLater()

    @classmethod
    def question(
        cls,
        parent,
        title: str,
        message: str,
        _buttons=None,
        default_button=QMessageBox.StandardButton.No,
    ):
        destructive = title.startswith("删除")
        result = cls._exec(
            FluentMessageDialog(
                title,
                message,
                parent,
                kind="question",
                accept_text="删除" if destructive else "确定",
                cancel_text="取消",
                destructive=destructive,
                default_accept=default_button == QMessageBox.StandardButton.Yes,
            )
        )
        return (
            QMessageBox.StandardButton.Yes
            if result == QDialog.DialogCode.Accepted
            else QMessageBox.StandardButton.No
        )

    @classmethod
    def information(cls, parent, title: str, message: str, *_args, **_kwargs):
        cls._exec(FluentMessageDialog(title, message, parent, kind="information"))
        return QMessageBox.StandardButton.Ok

    @classmethod
    def warning(cls, parent, title: str, message: str, *_args, **_kwargs):
        cls._exec(FluentMessageDialog(title, message, parent, kind="warning"))
        return QMessageBox.StandardButton.Ok

    @classmethod
    def critical(cls, parent, title: str, message: str, *_args, **_kwargs):
        cls._exec(FluentMessageDialog(title, message, parent, kind="critical"))
        return QMessageBox.StandardButton.Ok
