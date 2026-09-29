from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QRectF, QSize, QSizeF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap, QTextCursor, QTextOption
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .item_models import TYPE_LABELS, format_local_timestamp, human_size, normalized_thumbnail_path


def _item_file_present(path: str) -> bool:
    # Statting a dead network path can block the GUI thread for seconds; UNC
    # paths are treated as present and a missing file surfaces when the user
    # actually opens it.
    if path.startswith("\\\\"):
        return True
    return Path(path).exists()
from .detail_tag_grid import DetailTagGrid
from .detail_notes_state import DetailNotesState
from .markdown_view import SafeMarkdownBrowser, set_markdown_content
from .thumbnail_service import (
    ThumbnailDecodeQueue,
    ThumbnailCacheKey,
    cache_decoded_thumbnail,
    cached_thumbnail,
)
from .thumbnail_session import ThumbnailSession
from .ui_primitives import (
    AutoHideScrollBar,
    FluentComboBox,
    IconButton,
    ThemedSelectableLabel,
    ThemedTextEdit,
    lucide_icon,
)


def _wrap_detail_text(value: object, interval: int = 24) -> str:
    return str(value)


class _DetailCollectionCombo(FluentComboBox):
    """Collection picker rendered in the same widget tree as the details."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._popup_owner = None
        self.collection_popup = None
        self.collection_list = None

    def wheelEvent(self, event) -> None:
        # Choosing a collection writes to the library. A scroll gesture over
        # this field belongs to the enclosing details, never to selection.
        event.ignore()

    def showPopup(self) -> None:
        if not self.isEnabled() or not self.count():
            return
        owner = self.window()
        if self.collection_popup is None:
            self.collection_popup = QFrame(owner)
            self.destroyed.connect(self.collection_popup.deleteLater)
            self.collection_popup.setObjectName("DetailCollectionPopup")
            self.collection_popup.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
            layout = QVBoxLayout(self.collection_popup)
            layout.setContentsMargins(4, 4, 4, 4)
            self.collection_list = QListView(self.collection_popup)
            self.collection_list.setObjectName("DetailCollectionList")
            self.collection_list.setAccessibleName("选择收藏集")
            self.collection_list.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
            self.collection_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.collection_list.setVerticalScrollBar(AutoHideScrollBar(track_width=8, align_to_edge=True))
            self.collection_list.clicked.connect(self._choose_collection)
            layout.addWidget(self.collection_list)
        popup = self.collection_popup
        if popup.parentWidget() is not owner:
            popup.setParent(owner)
        self.collection_list.setModel(self.model())
        self.collection_list.setCurrentIndex(self.model().index(self.currentIndex(), self.modelColumn()))
        popup.ensurePolished()
        row_height = max(30, self.collection_list.sizeHintForRow(0))
        desired = min(8, self.count()) * row_height + 8
        anchor = self.mapTo(owner, QPoint(0, self.height()))
        below = max(0, owner.height() - anchor.y() - 4)
        above = max(0, anchor.y() - self.height() - 4)
        height = min(desired, max(below, above))
        top = anchor.y() if desired <= below or below >= above else anchor.y() - self.height() - height
        width = min(self.width(), owner.width() - 8)
        left = max(4, min(anchor.x(), owner.width() - width - 4))
        popup.setGeometry(left, max(4, top), max(1, width), max(1, height))
        popup.show()
        popup.raise_()
        self.collection_list.scrollTo(self.collection_list.currentIndex())
        self.collection_list.setFocus(Qt.FocusReason.PopupFocusReason)
        self._popup_owner = owner
        QApplication.instance().installEventFilter(self)

    def _choose_collection(self, index) -> None:
        if index.isValid():
            row = index.row()
            self.hidePopup()
            self.setCurrentIndex(row)
            self.activated.emit(row)

    def hidePopup(self) -> None:
        QApplication.instance().removeEventFilter(self)
        self._popup_owner = None
        if self.collection_popup is not None:
            focused = self.collection_popup.isAncestorOf(QApplication.focusWidget()) if QApplication.focusWidget() else False
            self.collection_popup.hide()
            if focused:
                self.setFocus(Qt.FocusReason.PopupFocusReason)
        super().hidePopup()

    def eventFilter(self, watched, event) -> bool:
        if self._popup_owner is not None:
            if watched is self._popup_owner and event.type() in (QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Hide, QEvent.Type.WindowDeactivate):
                self.hidePopup()
            elif event.type() == QEvent.Type.KeyPress:
                if event.key() == Qt.Key.Key_Escape:
                    self.hidePopup()
                    return True
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    self._choose_collection(self.collection_list.currentIndex())
                    return True
                if event.key() == Qt.Key.Key_Tab:
                    self.hidePopup()
            elif event.type() == QEvent.Type.MouseButtonPress and isinstance(watched, QWidget):
                inside = watched is self.collection_popup or self.collection_popup.isAncestorOf(watched)
                if not inside:
                    self.hidePopup()
                    if watched is self:
                        return True
        return super().eventFilter(watched, event)

    def hideEvent(self, event) -> None:
        self.hidePopup()
        super().hideEvent(event)

    def moveEvent(self, event) -> None:
        self.hidePopup()
        super().moveEvent(event)

    def resizeEvent(self, event) -> None:
        self.hidePopup()
        super().resizeEvent(event)


class _DetailMetadataValue(SafeMarkdownBrowser):
    def __init__(self, value: str):
        super().__init__()
        self.setObjectName("DetailValue")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.document().setDocumentMargin(0)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setPlainText(value)
        self.setToolTip(value)
        self.document().documentLayout().documentSizeChanged.connect(self._fit_height)

    def _fit_height(self, *_args) -> None:
        chrome = self.height() - self.viewport().height()
        height = math.ceil(self.document().size().height()) + chrome
        if height != self.height():
            self.setFixedHeight(height)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_height()


class _DetailMetadata(QWidget):
    """Selectable values in aligned rows; long paths may wrap without widening the pane."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 4, 0, 0)
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(10)
        self.grid.setColumnStretch(1, 1)

    def set_entries(self, entries: list[tuple[str, str]]) -> None:
        self.clear()
        self._text = "\n".join(f"{label}  {value}" for label, value in entries)
        for row, (label, value) in enumerate(entries):
            caption = QLabel(label)
            caption.setObjectName("DetailCaption")
            caption.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            text = _DetailMetadataValue(value)
            self.grid.addWidget(caption, row, 0)
            self.grid.addWidget(text, row, 1)

    def text(self) -> str:
        return self._text

    def clear(self) -> None:
        self._text = ""
        while self.grid.count():
            widget = self.grid.takeAt(0).widget()
            widget.hide()
            widget.deleteLater()


class _DetailImagePreview(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._source_pixmap = QPixmap()

    def set_source_pixmap(self, pixmap: QPixmap | None) -> None:
        self._source_pixmap = (
            QPixmap(pixmap)
            if pixmap is not None and not pixmap.isNull()
            else QPixmap()
        )
        super().clear()
        self.update()

    def clear(self) -> None:
        self._source_pixmap = QPixmap()
        super().clear()
        self.update()

    def pixmap(self) -> QPixmap:
        return self._source_pixmap

    def image_target_rect(self) -> QRectF:
        if self._source_pixmap.isNull():
            return QRectF()
        available = QSizeF(
            max(1, self.width()),
            max(1, min(230, self.height())),
        )
        source_size = self._source_pixmap.deviceIndependentSize()
        target_size = source_size.scaled(
            available,
            Qt.AspectRatioMode.KeepAspectRatio,
        )
        return QRectF(
            (self.width() - target_size.width()) / 2.0,
            (self.height() - target_size.height()) / 2.0,
            target_size.width(),
            target_size.height(),
        )

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._source_pixmap.isNull():
            return
        target = self.image_target_rect()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(
            target,
            self._source_pixmap,
            QRectF(self._source_pixmap.rect()),
        )
        painter.end()

class DetailPanel(QScrollArea):
    close_requested = Signal()
    copy_requested = Signal(int)
    open_requested = Signal(int)
    delete_requested = Signal(int)
    favorite_requested = Signal(int, bool)
    notes_changed = Signal(int, str)
    add_tag_requested = Signal(int)
    remove_tag_requested = Signal(int, str)
    collection_changed = Signal(int, object)
    ai_requested = Signal(int)
    ocr_requested = Signal(int)

    def _make_thumbnail_queue(self, parent) -> ThumbnailDecodeQueue:
        return ThumbnailDecodeQueue(parent)

    def _thumbnail_lookup(self, path, content_hash=None):
        return cached_thumbnail(path, content_hash)

    def _store_decoded_thumbnail(self, key, image):
        return cache_decoded_thumbnail(key, image)

    def _icon(self, name: str) -> QIcon:
        return lucide_icon(name)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DetailPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBar(
            AutoHideScrollBar(
                track_width=8,
                light_background="#f6f6f6",
                align_to_edge=True,
            )
        )
        self.setMinimumWidth(280)
        self.setMaximumWidth(340)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.current_item = None
        self._thumbnail_loader = self._make_thumbnail_queue(self)
        self._thumbnail_session = ThumbnailSession(self._thumbnail_loader)
        self._thumbnail_loader.decoded.connect(self._thumbnail_decoded)
        self._image_source_pixmap = QPixmap()
        self._width_transition_active = False
        self._width_transition_layout_frozen = False
        self._header_transition_width = 0
        self._preview_resize_timer = QTimer(self)
        self._preview_resize_timer.setSingleShot(True)
        self._preview_resize_timer.timeout.connect(self._resize_preview)

        # The header belongs to the scroll area, outside its viewport. Actions
        # remain reachable even when a long preview or a small screen scrolls.
        self.header = QFrame(self)
        self.header.setObjectName("DetailHeader")
        header_layout = QVBoxLayout(self.header)
        header_layout.setContentsMargins(16, 10, 16, 12)
        header_layout.setSpacing(6)
        top = QHBoxLayout()
        self.type_badge = QLabel("详情")
        self.type_badge.setObjectName("DetailHeading")
        top.addWidget(self.type_badge)
        top.addStretch()
        close = IconButton("close", "收起详情")
        close.clicked.connect(self.close_requested)
        top.addWidget(close)
        header_layout.addLayout(top)
        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.copy_button = QPushButton("复制内容")
        self.copy_button.setObjectName("DetailCopy")
        self.copy_button.setIcon(lucide_icon("copy", "#ffffff"))
        self.copy_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_button.clicked.connect(lambda: self.current_item and self.copy_requested.emit(self.current_item["id"]))
        self.open_button = IconButton("open", "打开内容")
        self.open_button.clicked.connect(lambda: self.current_item and self.open_requested.emit(self.current_item["id"]))
        self.favorite_button = IconButton("favorite", "收藏")
        self.favorite_button.setCheckable(True)
        self.favorite_button.clicked.connect(lambda: self.current_item and self.favorite_requested.emit(self.current_item["id"], not bool(self.current_item["favorite"])))
        self.delete_button = IconButton("delete", "删除")
        self.delete_button.clicked.connect(lambda: self.current_item and self.delete_requested.emit(self.current_item["id"]))
        self.item_action_buttons = [self.open_button, self.copy_button, self.favorite_button, self.delete_button]
        actions.addWidget(self.copy_button, 1)
        for button in (self.open_button, self.favorite_button, self.delete_button):
            actions.addWidget(button)
        header_layout.addLayout(actions)
        self.setViewportMargins(0, 96, 0, 0)

        self.content_widget = QWidget()
        self.content_widget.setObjectName("DetailPanelContent")
        self.content_widget.setMinimumWidth(0)
        layout = QVBoxLayout(self.content_widget)
        layout.setContentsMargins(16, 16, 16, 20)
        layout.setSpacing(18)
        self.title = QLabel("选择一项查看详情")
        self.title.setObjectName("DetailItemTitle")
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setWordWrap(True)
        self.title.setMinimumWidth(0)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.title)
        self.preview_section, preview_layout = self._section()
        self.preview_caption = self._caption("内容")
        preview_layout.addWidget(self.preview_caption)
        self.preview_stack = QStackedWidget()
        self.preview_stack.setMinimumWidth(0)
        self.preview_stack.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.image_preview = _DetailImagePreview()
        self.image_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_preview.setMinimumWidth(0)
        self.image_preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.image_preview.setObjectName("DetailPreview")
        self.text_preview = SafeMarkdownBrowser()
        self.text_preview.setObjectName("DetailTextPreview")
        self.text_preview.setAccessibleName("内容预览")
        self.text_preview.setOpenExternalLinks(False)
        self.text_preview.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.text_preview.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.text_preview.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.text_preview.setVerticalScrollBar(AutoHideScrollBar(track_width=8, light_background="#ffffff", align_to_edge=True))
        self._text_preview_viewport = self.text_preview.viewport()
        self.text_preview.document().documentLayout().documentSizeChanged.connect(self._schedule_preview_resize)
        self.preview_stack.addWidget(self.image_preview)
        self.preview_stack.addWidget(self.text_preview)
        preview_layout.addWidget(self.preview_stack)
        layout.addWidget(self.preview_section)

        self.organize_section, organize_layout = self._section()
        organize_layout.addWidget(self._caption("收藏集"))
        self.collection_combo = _DetailCollectionCombo()
        self.collection_combo.addItem("未分类", None)
        self.collection_combo.setMinimumWidth(0)
        self.collection_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.collection_combo.setAccessibleName("所属收藏集")
        self.collection_combo.currentIndexChanged.connect(self._collection_changed)
        self.verticalScrollBar().valueChanged.connect(self.collection_combo.hidePopup)
        organize_layout.addWidget(self.collection_combo)
        organize_layout.addSpacing(6)

        tag_title = QHBoxLayout()
        tag_title.addWidget(self._caption("标签"))
        tag_title.addStretch()
        add_tag = IconButton("add", "添加标签")
        self.add_tag_button = add_tag
        add_tag.clicked.connect(lambda: self.current_item and self.add_tag_requested.emit(self.current_item["id"]))
        tag_title.addWidget(add_tag)
        organize_layout.addLayout(tag_title)
        self.tags_empty = QLabel("暂无标签")
        self.tags_empty.setObjectName("DetailCaption")
        organize_layout.addWidget(self.tags_empty)
        self.tag_grid = DetailTagGrid()
        self.tag_grid.remove_requested.connect(
            lambda tag: self.current_item
            and self.remove_tag_requested.emit(self.current_item["id"], tag)
        )
        organize_layout.addWidget(self.tag_grid)
        layout.addWidget(self.organize_section)

        self.image_tools, tools_layout = self._section()
        ocr_row = QHBoxLayout()
        ocr_row.addWidget(self._caption("图片文字"))
        ocr_row.addStretch()
        self.ocr_button = QPushButton("识别文字")
        self.ocr_button.setObjectName("DetailToolAction")
        self.ocr_button.setIcon(self._icon("scan-text"))
        self.ocr_button.setToolTip("使用设置中的视觉模型识别图片文字")
        self.ocr_button.clicked.connect(lambda: self.current_item and self.ocr_requested.emit(self.current_item["id"]))
        ocr_row.addWidget(self.ocr_button)
        tools_layout.addLayout(ocr_row)
        self.ocr_text = ThemedSelectableLabel("尚未识别")
        self.ocr_text.setObjectName("DetailResult")
        self.ocr_text.setTextFormat(Qt.TextFormat.PlainText)
        self.ocr_text.setWordWrap(True)
        self.ocr_text.setMinimumWidth(0)
        self.ocr_text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.ocr_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        tools_layout.addWidget(self.ocr_text)

        ai_row = QHBoxLayout()
        ai_row.addWidget(self._caption("图片描述"))
        ai_row.addStretch()
        self.ai_button = QPushButton("生成描述")
        self.ai_button.setObjectName("DetailToolAction")
        self.ai_button.setIcon(self._icon("sparkles"))
        self.ai_button.clicked.connect(lambda: self.current_item and self.ai_requested.emit(self.current_item["id"]))
        ai_row.addWidget(self.ai_button)
        tools_layout.addLayout(ai_row)
        self.ai_description = ThemedSelectableLabel("尚未生成")
        self.ai_description.setObjectName("DetailResult")
        self.ai_description.setTextFormat(Qt.TextFormat.PlainText)
        self.ai_description.setWordWrap(True)
        self.ai_description.setMinimumWidth(0)
        self.ai_description.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.ai_description.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        tools_layout.addWidget(self.ai_description)
        # Keep image tools immediately after the preview.
        layout.insertWidget(2, self.image_tools)

        self.notes_section, notes_layout = self._section()
        notes_layout.addWidget(self._caption("备注"))
        self.notes = ThemedTextEdit()
        self.notes.setObjectName("DetailNotes")
        self.notes.setAccessibleName("备注")
        self.notes.setPlaceholderText("添加备注…")
        self.notes.setFixedHeight(84)
        self.notes.installEventFilter(self)
        self._notes_state = DetailNotesState()
        self._note_drafts = self._notes_state.drafts
        self._note_draft_bases = self._notes_state.bases
        notes_layout.addWidget(self.notes)
        layout.addWidget(self.notes_section)
        self.info_section, info_layout = self._section()
        self.info_toggle = QPushButton("详细信息")
        self.info_toggle.setObjectName("DetailInfoToggle")
        self.info_toggle.setCheckable(True)
        self.info_toggle.toggled.connect(self._toggle_metadata)
        info_layout.addWidget(self.info_toggle)
        self.meta = _DetailMetadata()
        info_layout.addWidget(self.meta)
        self._toggle_metadata(False)
        layout.addWidget(self.info_section)
        layout.addStretch()
        self.setWidget(self.content_widget)
        self._text_preview_viewport.installEventFilter(self)
        self.clear_item()

    @staticmethod
    def _section() -> tuple[QWidget, QVBoxLayout]:
        section = QWidget()
        section.setMinimumWidth(0)
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        return section, layout

    @staticmethod
    def _caption(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("DetailSectionTitle")
        return label

    def _toggle_metadata(self, expanded: bool) -> None:
        self.meta.setVisible(expanded)
        self.info_toggle.setIcon(self._icon("chevron-down" if expanded else "chevron-right"))

    def refresh_theme(self) -> None:
        self.ai_button.setIcon(self._icon("sparkles"))
        self.ocr_button.setIcon(self._icon("scan-text"))
        self._toggle_metadata(self.info_toggle.isChecked())

    def _schedule_preview_resize(self, *_args) -> None:
        if not self._width_transition_active:
            self._preview_resize_timer.start(0)

    def _resize_preview(self) -> None:
        if self._width_transition_layout_frozen:
            return
        if self.preview_stack.currentWidget() is self.image_preview:
            height = 220
        else:
            chrome = self.text_preview.height() - self.text_preview.viewport().height()
            height = max(72, min(280, math.ceil(self.text_preview.document().size().height()) + chrome + 2))
        if self.preview_stack.height() != height:
            self.preview_stack.setFixedHeight(height)

    def _position_header(self) -> None:
        # Freeze the header's width too while the splitter reveals the panel.
        width = self._header_transition_width if self._width_transition_active else self.width()
        height = max(96, self.header.sizeHint().height())
        if self.viewportMargins().top() != height:
            self.setViewportMargins(0, height, 0, 0)
        self.header.setGeometry(0, 0, width, height)
        self.header.raise_()

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        if self._width_transition_active:
            return QSize(0, hint.height())
        return hint

    def _set_width_transition_layout_frozen(self, frozen: bool) -> None:
        layout = self.content_widget.layout()
        frozen = bool(frozen)
        if self._width_transition_layout_frozen == frozen:
            return
        self._width_transition_layout_frozen = frozen
        layout.setEnabled(not frozen)
        if not frozen:
            layout.activate()

    def begin_width_transition(
        self,
        final_panel_width: int,
        final_content_width: int | None = None,
    ) -> None:
        final_panel_width = max(1, int(final_panel_width))
        self._set_width_transition_layout_frozen(False)
        self._preview_resize_timer.stop()
        self._width_transition_active = True
        self._header_transition_width = final_panel_width
        self.setWidgetResizable(False)
        content_width = max(
            1,
            int(final_content_width)
            if final_content_width is not None
            else final_panel_width
            - self.verticalScrollBar().sizeHint().width(),
        )
        self.content_widget.setFixedWidth(content_width)
        self._position_header()
        self.content_widget.layout().activate()
        self.content_widget.resize(
            content_width,
            max(
                self.viewport().height(),
                self.content_widget.sizeHint().height(),
            ),
        )
        self._refresh_image_preview()

    def synchronize_width_transition_content(self) -> None:
        if not self._width_transition_active:
            return
        content_width = max(1, self.viewport().width())
        self.content_widget.setFixedWidth(content_width)
        self._position_header()
        self._resize_preview()
        self.content_widget.layout().activate()
        self.content_widget.resize(
            content_width,
            max(
                self.viewport().height(),
                self.content_widget.sizeHint().height(),
            ),
        )
        self._set_width_transition_layout_frozen(True)

    def finish_width_transition(self) -> None:
        if not self._width_transition_active:
            return
        self._set_width_transition_layout_frozen(False)
        self._width_transition_active = False
        self.content_widget.setMinimumWidth(0)
        self.content_widget.setMaximumWidth(16_777_215)
        self.setWidgetResizable(True)
        self.content_widget.updateGeometry()
        self._position_header()
        self._schedule_preview_resize()
        self._refresh_image_preview()

    def set_collections(self, collections) -> None:
        self.collection_combo.hidePopup()
        selected = self.collection_combo.currentData()
        self.collection_combo.blockSignals(True)
        self.collection_combo.clear()
        self.collection_combo.addItem("未分类", None)
        for collection in collections:
            self.collection_combo.addItem(collection["name"], collection["id"])
        index = self.collection_combo.findData(selected)
        self.collection_combo.setCurrentIndex(max(0, index))
        self.collection_combo.blockSignals(False)

    def set_item(self, item) -> bool:
        previous_item_id = self.current_item["id"] if self.current_item else None
        loaded_notes_before_flush = self._notes_state.loaded_notes
        self.flush_notes()
        if previous_item_id is not None and (
            self.current_item is None or self.current_item["id"] != previous_item_id
        ):
            return False
        if self._notes_state.loaded_notes != loaded_notes_before_flush:
            return False
        same_text_preview = bool(
            self.current_item is not None
            and previous_item_id == item["id"]
            and item["kind"] in ("text", "markdown")
            and self.current_item["kind"] == item["kind"]
            and self.current_item["content"] == item["content"]
        )
        self._thumbnail_session.invalidate()
        if previous_item_id != item["id"]:
            self.tag_grid.reset_expanded()
        self.collection_combo.hidePopup()
        self.current_item = item
        self._image_source_pixmap = QPixmap()
        self.image_preview.clear()
        if not same_text_preview:
            self.text_preview.clear()
        kind_label = TYPE_LABELS.get(item["kind"], item["kind"])
        self.type_badge.setText(f"{kind_label}详情")
        self.title.setText(_wrap_detail_text(item["title"]))
        self.title.setToolTip(item["title"])
        first_line = next((line.strip() for line in (item["content"] or "").splitlines() if line.strip()), "")
        duplicate_title = item["kind"] in ("text", "markdown") and item["title"] == first_line[:80]
        self.title.setVisible(bool(item["title"]) and not duplicate_title)
        for section in (self.preview_section, self.organize_section, self.notes_section, self.info_section):
            section.show()
        image_kind = item["kind"] == "image"
        file_present = bool(item["path"]) and _item_file_present(item["path"])
        self.preview_caption.setText("图片预览" if image_kind else "内容")
        self.copy_button.setText("复制图片" if image_kind else "复制内容")
        self.open_button.setToolTip("打开图片" if image_kind else "打开内容")
        self.open_button.setAccessibleName(self.open_button.toolTip())
        self.image_tools.setVisible(image_kind)
        if image_kind:
            self.preview_stack.setCurrentWidget(self.image_preview)
            self.image_preview.setText("正在加载图片…" if file_present else "原图片已移动或删除")
            if file_present:
                try:
                    content_hash = item["content_hash"]
                except (KeyError, IndexError):
                    content_hash = None
                key, pixmap, cached = self._thumbnail_lookup(item["path"], content_hash)
                if cached:
                    self._set_image_preview(pixmap)
                elif key is not None:
                    self._thumbnail_session.request(key)
                else:
                    self.image_preview.setText("无法预览这张图片")
        else:
            if not same_text_preview:
                if item["kind"] == "markdown":
                    set_markdown_content(self.text_preview, item["content"])
                else:
                    self.text_preview.setPlainText(item["content"])
                self.text_preview.moveCursor(QTextCursor.MoveOperation.Start)
            self.preview_stack.setCurrentWidget(self.text_preview)
        first_seen = format_local_timestamp(item["created_at"])
        last_used = format_local_timestamp(item["last_used_at"])
        entries = [("类型", kind_label), ("大小", human_size(item["file_size"]))]
        if item["width"]:
            entries.append(("尺寸", f"{item['width']} × {item['height']}"))
        entries.append(("保存时间", first_seen))
        if first_seen != last_used:
            entries.append(("最近使用", last_used))
        entries.append(("来源", item["source"] or "未知"))
        if item["path"]:
            entries.append(("路径", str(item["path"])))
        self.meta.set_entries(entries)
        self.meta.setToolTip(item["path"] or "")
        self.collection_combo.blockSignals(True)
        index = self.collection_combo.findData(item["collection_id"])
        self.collection_combo.setCurrentIndex(max(0, index))
        self.collection_combo.blockSignals(False)
        self.collection_combo.setEnabled(True)
        self.add_tag_button.setEnabled(True)
        self.tag_grid.set_tags(item["tag_names"] or "", item["tag_colors"] or "")
        self.tags_empty.setVisible(not bool(item["tag_names"]))
        self.tag_grid.setVisible(bool(item["tag_names"]))
        self.ai_description.setText(item["ai_description"] or "")
        self.ai_description.setVisible(bool(item["ai_description"]))
        self.ocr_text.setText(item["ocr_text"] or "")
        self.ocr_text.setVisible(bool(item["ocr_text"]))
        is_image = image_kind and file_present
        self.ai_button.setEnabled(is_image)
        self.ai_button.setText("重新生成" if item["ai_description"] else "生成描述")
        self.ocr_button.setEnabled(is_image)
        self.ocr_button.setText("重新识别" if item["ocr_text"] else "识别文字")
        for button in self.item_action_buttons:
            button.setEnabled(True)
        self.favorite_button.setChecked(bool(item["favorite"]))
        favorite_label = "取消收藏" if item["favorite"] else "收藏"
        self.favorite_button.setToolTip(favorite_label)
        self.favorite_button.setAccessibleName(favorite_label)
        self.open_button.setEnabled(not image_kind or file_present)
        self.copy_button.setEnabled(not image_kind or file_present)
        self.notes.blockSignals(True)
        loaded_notes = item["notes"] or ""
        display_notes = self._notes_state.display_notes(item["id"], loaded_notes)
        if previous_item_id != item["id"] or self.notes.toPlainText() != display_notes:
            self.notes.setPlainText(display_notes)
        self.notes.blockSignals(False)
        self.notes.setEnabled(True)
        self._resize_preview()
        self._schedule_preview_resize()
        if previous_item_id != item["id"]:
            self.verticalScrollBar().setValue(0)
        return True

    def clear_item(self) -> None:
        self.flush_notes()
        self._thumbnail_session.invalidate()
        self.current_item = None
        self._image_source_pixmap = QPixmap()
        self.type_badge.setText("详情")
        self.title.setText("选择一项查看详情")
        self.title.show()
        self.title.setToolTip("")
        self.image_preview.clear()
        self.text_preview.clear()
        self.preview_stack.setCurrentWidget(self.text_preview)
        self.meta.clear()
        self.meta.setToolTip("")
        self.tag_grid.clear_tags()
        for section in (self.preview_section, self.organize_section, self.image_tools, self.notes_section, self.info_section):
            section.hide()
        self.ai_description.setText("尚未生成")
        self.ocr_text.setText("尚未识别")
        self.ai_button.setEnabled(False)
        self.ai_button.setText("生成描述")
        self.ocr_button.setEnabled(False)
        self.ocr_button.setText("识别文字")
        self.collection_combo.blockSignals(True)
        self.collection_combo.setCurrentIndex(0)
        self.collection_combo.blockSignals(False)
        self.collection_combo.setEnabled(False)
        self.add_tag_button.setEnabled(False)
        self.notes.blockSignals(True)
        self.notes.clear()
        self.notes.blockSignals(False)
        self.notes.setEnabled(False)
        self._notes_state.clear_current()
        for button in self.item_action_buttons:
            button.setEnabled(False)
        self.favorite_button.setChecked(False)

    def closeEvent(self, event) -> None:
        self._thumbnail_session.close()
        super().closeEvent(event)

    def shutdown_thumbnail_loader(self, timeout_ms: int = 2000) -> bool:
        return self._thumbnail_session.close(timeout_ms)

    def wait_for_thumbnail_idle(self) -> bool:
        return self._thumbnail_session.pause_and_wait()

    def resume_thumbnail_loader(self) -> None:
        self._thumbnail_session.resume()
        if self.current_item is not None:
            self.set_item(self.current_item)

    @Slot(object, object, int)
    def _thumbnail_decoded(self, key: ThumbnailCacheKey, image: QImage, generation: int) -> None:
        if not self._thumbnail_session.is_current(generation) or self.current_item is None:
            return
        if normalized_thumbnail_path(self.current_item["path"]) != key.path:
            return
        pixmap = self._store_decoded_thumbnail(key, image)
        if pixmap is not None:
            self._set_image_preview(pixmap)

    def _set_image_preview(self, pixmap: QPixmap | None) -> None:
        if pixmap is None or pixmap.isNull():
            self.image_preview.setText("无法预览这张图片")
            return
        self._image_source_pixmap = QPixmap(pixmap)
        self.image_preview.set_source_pixmap(self._image_source_pixmap)

    def _refresh_image_preview(self) -> None:
        if self._image_source_pixmap.isNull():
            return
        self.image_preview.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._position_header()
        if not self._image_source_pixmap.isNull():
            self._refresh_image_preview()

    def set_ai_busy(self, busy: bool, failed: bool = False) -> None:
        is_image = bool(
            self.current_item
            and self.current_item["kind"] == "image"
            and self.current_item["path"]
            and _item_file_present(self.current_item["path"])
        )
        self.ai_button.setEnabled(is_image and not busy)
        self.ai_button.setText("生成中…" if busy else "重试" if failed else "生成描述")

    def set_ocr_busy(self, busy: bool, failed: bool = False) -> None:
        is_image = bool(
            self.current_item
            and self.current_item["kind"] == "image"
            and self.current_item["path"]
            and _item_file_present(self.current_item["path"])
        )
        self.ocr_button.setEnabled(is_image and not busy)
        self.ocr_button.setText("识别中…" if busy else "重试" if failed else "识别文字")

    @property
    def tags_box(self):
        return self.tag_grid.grid

    @property
    def tags_more_button(self) -> QPushButton | None:
        return self.tag_grid.more_button

    def _collection_changed(self, _index: int) -> None:
        if self.current_item:
            self.collection_changed.emit(self.current_item["id"], self.collection_combo.currentData())

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.Resize and watched is self._text_preview_viewport:
            self._schedule_preview_resize()
        if event.type() == QEvent.Type.FocusOut and watched is self.notes and self.current_item:
            self.flush_notes()
        return super().eventFilter(watched, event)

    def flush_notes(self) -> bool:
        if not self.current_item:
            return True
        item_id = self.current_item["id"]
        notes = self.notes.toPlainText()
        if notes == self._notes_state.loaded_notes:
            self._notes_state.stage(item_id, notes)
            return True
        if self._notes_state.stage(item_id, notes):
            self.notes_changed.emit(item_id, notes)
        return self._note_drafts.get(item_id) != notes

    def mark_notes_saved(self, item_id: int, notes: str) -> None:
        self._notes_state.mark_saved(
            item_id,
            notes,
            update_loaded=bool(
                self.current_item
                and self.current_item["id"] == item_id
                and self.notes.toPlainText() == notes
            ),
        )

    def pending_note_drafts(self) -> dict[int, str]:
        return self._notes_state.pending_drafts()

    def pending_note_updates(self) -> dict[int, tuple[str, str]]:
        return self._notes_state.pending_updates()

    @property
    def loaded_notes(self) -> str:
        return self._notes_state.loaded_notes
