from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QRectF, QSize, QSizeF, Qt, Signal, Slot
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .item_models import TYPE_LABELS, format_local_timestamp, human_size, normalized_thumbnail_path
from .markdown_view import _SafeMarkdownBrowser, _set_markdown_content
from .thumbnail_service import (
    ThumbnailDecodeQueue,
    _ThumbnailCacheKey,
    _cache_decoded_thumbnail,
    _cached_thumbnail,
)
from .ui_primitives import (
    AutoHideScrollBar,
    FluentComboBox,
    IconButton,
    ThemedSelectableLabel,
    ThemedTextEdit,
    color_dot,
    lucide_icon,
)


def _wrap_detail_text(value: object, interval: int = 24) -> str:
    return str(value)

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
        return _cached_thumbnail(path, content_hash)

    def _store_decoded_thumbnail(self, key, image):
        return _cache_decoded_thumbnail(key, image)

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
        self._thumbnail_generation = 0
        self._thumbnail_loader = self._make_thumbnail_queue(self)
        self._thumbnail_loader.decoded.connect(self._thumbnail_decoded)
        self._image_source_pixmap = QPixmap()
        self._tag_names: list[str] = []
        self._tag_colors: list[str] = []
        self._tags_expanded = False
        self._width_transition_active = False
        self._width_transition_layout_frozen = False
        self.tags_more_button: QPushButton | None = None
        self.content_widget = QWidget()
        self.content_widget.setObjectName("DetailPanelContent")
        self.content_widget.setMinimumWidth(0)
        layout = QVBoxLayout(self.content_widget)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(10)
        top = QHBoxLayout()
        self.type_badge = QLabel("详情")
        self.type_badge.setObjectName("Muted")
        top.addWidget(self.type_badge)
        top.addStretch()
        close = IconButton("close", "收起详情")
        close.clicked.connect(self.close_requested)
        top.addWidget(close)
        layout.addLayout(top)
        self.title = QLabel("选择一项查看详情")
        self.title.setObjectName("Title")
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setWordWrap(True)
        self.title.setMinimumWidth(0)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.title)
        self.preview_stack = QStackedWidget()
        self.image_preview = _DetailImagePreview()
        self.image_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_preview.setMinimumHeight(190)
        self.image_preview.setMinimumWidth(0)
        self.image_preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.image_preview.setObjectName("DetailPreview")
        self.text_preview = _SafeMarkdownBrowser()
        self.text_preview.setOpenExternalLinks(False)
        self.text_preview.setMinimumHeight(190)
        self.preview_stack.addWidget(self.image_preview)
        self.preview_stack.addWidget(self.text_preview)
        layout.addWidget(self.preview_stack, 1)
        self.meta = ThemedSelectableLabel()
        self.meta.setTextFormat(Qt.TextFormat.PlainText)
        self.meta.setWordWrap(False)
        self.meta.setMinimumWidth(0)
        self.meta.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.meta.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.meta)

        collection_row = QHBoxLayout()
        collection_row.addWidget(QLabel("集合"))
        self.collection_combo = FluentComboBox()
        self.collection_combo.currentIndexChanged.connect(self._collection_changed)
        collection_row.addWidget(self.collection_combo, 1)
        layout.addLayout(collection_row)

        tag_title = QHBoxLayout()
        tag_title.addWidget(QLabel("标签"))
        tag_title.addStretch()
        add_tag = IconButton("add", "添加标签")
        self.add_tag_button = add_tag
        add_tag.clicked.connect(lambda: self.current_item and self.add_tag_requested.emit(self.current_item["id"]))
        tag_title.addWidget(add_tag)
        layout.addLayout(tag_title)
        self.tags_box = QGridLayout()
        self.tags_box.setHorizontalSpacing(4)
        self.tags_box.setVerticalSpacing(4)
        self.tags_box.setColumnStretch(0, 1)
        self.tags_box.setColumnStretch(1, 1)
        layout.addLayout(self.tags_box)

        ocr_row = QHBoxLayout()
        ocr_row.addWidget(QLabel("OCR 文字"))
        ocr_row.addStretch()
        self.ocr_button = QPushButton("识别文字")
        self.ocr_button.setIcon(self._icon("scan-text"))
        self.ocr_button.setToolTip("使用设置中的视觉模型识别图片文字")
        self.ocr_button.clicked.connect(lambda: self.current_item and self.ocr_requested.emit(self.current_item["id"]))
        ocr_row.addWidget(self.ocr_button)
        layout.addLayout(ocr_row)
        self.ocr_text = ThemedSelectableLabel("尚未识别")
        self.ocr_text.setObjectName("Muted")
        self.ocr_text.setTextFormat(Qt.TextFormat.PlainText)
        self.ocr_text.setWordWrap(True)
        self.ocr_text.setMinimumWidth(0)
        self.ocr_text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.ocr_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.ocr_text)

        ai_row = QHBoxLayout()
        ai_row.addWidget(QLabel("AI 描述"))
        ai_row.addStretch()
        self.ai_button = QPushButton("生成描述")
        self.ai_button.setIcon(self._icon("sparkles"))
        self.ai_button.clicked.connect(lambda: self.current_item and self.ai_requested.emit(self.current_item["id"]))
        ai_row.addWidget(self.ai_button)
        layout.addLayout(ai_row)
        self.ai_description = ThemedSelectableLabel("尚未生成")
        self.ai_description.setObjectName("Muted")
        self.ai_description.setTextFormat(Qt.TextFormat.PlainText)
        self.ai_description.setWordWrap(True)
        self.ai_description.setMinimumWidth(0)
        self.ai_description.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.ai_description.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.ai_description)

        layout.addWidget(QLabel("备注"))
        self.notes = ThemedTextEdit()
        self.notes.setPlaceholderText("添加备注…")
        self.notes.setMaximumHeight(90)
        self.notes.installEventFilter(self)
        self._loaded_notes = ""
        self._note_drafts: dict[int, str] = {}
        self._note_draft_bases: dict[int, str] = {}
        layout.addWidget(self.notes)
        actions = QHBoxLayout()
        self.item_action_buttons = []
        for glyph, tooltip, signal in [
            ("open", "打开", self.open_requested),
            ("copy", "复制", self.copy_requested),
            ("favorite", "收藏", None),
            ("delete", "删除", self.delete_requested),
        ]:
            button = IconButton(glyph, tooltip)
            if signal:
                button.clicked.connect(lambda _checked=False, s=signal: self.current_item and s.emit(self.current_item["id"]))
            else:
                button.clicked.connect(lambda: self.current_item and self.favorite_requested.emit(self.current_item["id"], not bool(self.current_item["favorite"])))
            self.item_action_buttons.append(button)
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.setWidget(self.content_widget)
        self.clear_item()

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
        self._width_transition_active = True
        self.setWidgetResizable(False)
        content_width = max(
            1,
            int(final_content_width)
            if final_content_width is not None
            else final_panel_width
            - self.verticalScrollBar().sizeHint().width(),
        )
        self.content_widget.setFixedWidth(content_width)
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
        self._refresh_image_preview()

    def set_collections(self, collections) -> None:
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
        loaded_notes_before_flush = self._loaded_notes
        self.flush_notes()
        if previous_item_id is not None and (
            self.current_item is None or self.current_item["id"] != previous_item_id
        ):
            return False
        if self._loaded_notes != loaded_notes_before_flush:
            return False
        self._thumbnail_generation += 1
        self._thumbnail_loader.cancel_queued()
        if previous_item_id != item["id"]:
            self._tags_expanded = False
        self.current_item = item
        self._image_source_pixmap = QPixmap()
        self.image_preview.clear()
        self.text_preview.clear()
        self.type_badge.setText(TYPE_LABELS.get(item["kind"], item["kind"]))
        self.title.setText(_wrap_detail_text(item["title"]))
        self.title.setToolTip(item["title"])
        if item["kind"] == "image" and item["path"] and Path(item["path"]).exists():
            self.preview_stack.setCurrentWidget(self.image_preview)
            try:
                content_hash = item["content_hash"]
            except (KeyError, IndexError):
                content_hash = None
            key, pixmap, cached = self._thumbnail_lookup(
                item["path"],
                content_hash,
            )
            if cached:
                self._set_image_preview(pixmap)
            elif key is not None:
                self._thumbnail_loader.request(key, self._thumbnail_generation)
        else:
            if item["kind"] == "markdown":
                _set_markdown_content(self.text_preview, item["content"])
            else:
                self.text_preview.setPlainText(item["content"])
            self.preview_stack.setCurrentWidget(self.text_preview)
        dimensions = f"{item['width']} × {item['height']}\n" if item["width"] else ""
        path_text = f"\n路径  {_wrap_detail_text(item['path'])}" if item["path"] else ""
        self.meta.setText(f"类型  {TYPE_LABELS.get(item['kind'], item['kind'])}\n{dimensions}大小  {human_size(item['file_size'])}\n时间  {format_local_timestamp(item['created_at'])}\n来源  {item['source']}{path_text}")
        self.meta.setToolTip(item["path"] or "")
        self.collection_combo.blockSignals(True)
        index = self.collection_combo.findData(item["collection_id"])
        self.collection_combo.setCurrentIndex(max(0, index))
        self.collection_combo.blockSignals(False)
        self.collection_combo.setEnabled(True)
        self.add_tag_button.setEnabled(True)
        self._set_tags(item["tag_names"] or "", item["tag_colors"] or "")
        self.ai_description.setText(
            _wrap_detail_text(item["ai_description"]) if item["ai_description"] else "尚未生成"
        )
        self.ocr_text.setText(
            _wrap_detail_text(item["ocr_text"]) if item["ocr_text"] else "尚未识别"
        )
        is_image = item["kind"] == "image" and bool(item["path"]) and Path(item["path"]).exists()
        self.ai_button.setEnabled(is_image)
        self.ai_button.setText("重新生成" if item["ai_description"] else "生成描述")
        self.ocr_button.setEnabled(is_image)
        self.ocr_button.setText("重新识别" if item["ocr_text"] else "识别文字")
        for button in self.item_action_buttons:
            button.setEnabled(True)
        self.notes.blockSignals(True)
        loaded_notes = item["notes"] or ""
        self.notes.setPlainText(self._note_drafts.get(item["id"], loaded_notes))
        self.notes.blockSignals(False)
        self.notes.setEnabled(True)
        self._loaded_notes = loaded_notes
        return True

    def clear_item(self) -> None:
        self.flush_notes()
        self._thumbnail_generation += 1
        self._thumbnail_loader.cancel_queued()
        self.current_item = None
        self._image_source_pixmap = QPixmap()
        self.type_badge.setText("详情")
        self.title.setText("选择一项查看详情")
        self.title.setToolTip("")
        self.image_preview.clear()
        self.text_preview.clear()
        self.preview_stack.setCurrentWidget(self.text_preview)
        self.meta.clear()
        self.meta.setToolTip("")
        self._set_tags("", "")
        self._tags_expanded = False
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
        self._loaded_notes = ""
        for button in self.item_action_buttons:
            button.setEnabled(False)

    def closeEvent(self, event) -> None:
        self._thumbnail_generation += 1
        self._thumbnail_loader.close()
        super().closeEvent(event)

    def shutdown_thumbnail_loader(self, timeout_ms: int = 2000) -> bool:
        self._thumbnail_generation += 1
        return self._thumbnail_loader.close(timeout_ms)

    def wait_for_thumbnail_idle(self) -> bool:
        self._thumbnail_generation += 1
        return self._thumbnail_loader.pause_and_wait()

    def resume_thumbnail_loader(self) -> None:
        self._thumbnail_loader.resume()
        if self.current_item is not None:
            self.set_item(self.current_item)

    @Slot(object, object, int)
    def _thumbnail_decoded(self, key: _ThumbnailCacheKey, image: QImage, generation: int) -> None:
        if generation != self._thumbnail_generation or self.current_item is None:
            return
        if normalized_thumbnail_path(self.current_item["path"]) != key.path:
            return
        pixmap = self._store_decoded_thumbnail(key, image)
        if pixmap is not None:
            self._set_image_preview(pixmap)

    def _set_image_preview(self, pixmap: QPixmap | None) -> None:
        if pixmap is None or pixmap.isNull():
            return
        self._image_source_pixmap = QPixmap(pixmap)
        self.image_preview.set_source_pixmap(self._image_source_pixmap)

    def _refresh_image_preview(self) -> None:
        if self._image_source_pixmap.isNull():
            return
        self.image_preview.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if not self._image_source_pixmap.isNull():
            self._refresh_image_preview()

    def set_ai_busy(self, busy: bool, failed: bool = False) -> None:
        is_image = bool(
            self.current_item
            and self.current_item["kind"] == "image"
            and self.current_item["path"]
            and Path(self.current_item["path"]).exists()
        )
        self.ai_button.setEnabled(is_image and not busy)
        self.ai_button.setText("生成中…" if busy else "重试" if failed else "生成描述")

    def set_ocr_busy(self, busy: bool, failed: bool = False) -> None:
        is_image = bool(
            self.current_item
            and self.current_item["kind"] == "image"
            and self.current_item["path"]
            and Path(self.current_item["path"]).exists()
        )
        self.ocr_button.setEnabled(is_image and not busy)
        self.ocr_button.setText("识别中…" if busy else "重试" if failed else "识别文字")

    def _set_tags(self, names: str, colors: str) -> None:
        while self.tags_box.count():
            item = self.tags_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._tag_names = names.split("\x1f") if names else []
        self._tag_colors = colors.split("\x1f") if colors else []
        self.tags_more_button = None
        visible_names = self._tag_names if self._tags_expanded else self._tag_names[:4]
        for index, name in enumerate(visible_names):
            button = QPushButton()
            button.setObjectName("TagChip")
            button.setText(button.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, 108))
            button.setMinimumWidth(0)
            button.setMaximumWidth(130)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            color = self._tag_colors[index] if index < len(self._tag_colors) else "#64748b"
            button.setIcon(QIcon(color_dot(color)))
            button.setToolTip(f"{name}\n点击移除标签")
            button.clicked.connect(lambda _checked=False, tag=name: self.current_item and self.remove_tag_requested.emit(self.current_item["id"], tag))
            self.tags_box.addWidget(button, index // 2, index % 2)
        if len(self._tag_names) > 4:
            more = QPushButton()
            more.setObjectName("TagMoreButton")
            if self._tags_expanded:
                more.setText("收起标签")
                more.setToolTip("仅显示前四个标签")
            else:
                more.setText(f"更多标签  +{len(self._tag_names) - 4}")
                more.setToolTip("显示全部标签")
            more.clicked.connect(self._toggle_tags_expanded)
            self.tags_box.addWidget(more, (len(visible_names) + 1) // 2, 0, 1, 2)
            self.tags_more_button = more

    def _toggle_tags_expanded(self) -> None:
        self._tags_expanded = not self._tags_expanded
        self._set_tags("\x1f".join(self._tag_names), "\x1f".join(self._tag_colors))

    def _collection_changed(self, _index: int) -> None:
        if self.current_item:
            self.collection_changed.emit(self.current_item["id"], self.collection_combo.currentData())

    def eventFilter(self, watched, event) -> bool:
        if watched is self.notes and event.type() == QEvent.Type.FocusOut and self.current_item:
            self.flush_notes()
        return super().eventFilter(watched, event)

    def flush_notes(self) -> bool:
        if not self.current_item:
            return True
        item_id = self.current_item["id"]
        notes = self.notes.toPlainText()
        if notes == self._loaded_notes:
            self._note_drafts.pop(item_id, None)
            self._note_draft_bases.pop(item_id, None)
            return True
        if notes != self._loaded_notes:
            self._note_draft_bases.setdefault(item_id, self._loaded_notes)
            self._note_drafts[item_id] = notes
            self.notes_changed.emit(item_id, notes)
        return self._note_drafts.get(item_id) != notes

    def mark_notes_saved(self, item_id: int, notes: str) -> None:
        if self.current_item and self.current_item["id"] == item_id and self.notes.toPlainText() == notes:
            self._loaded_notes = notes
        if self._note_drafts.get(item_id) == notes:
            self._note_drafts.pop(item_id, None)
            self._note_draft_bases.pop(item_id, None)

    def pending_note_drafts(self) -> dict[int, str]:
        return dict(self._note_drafts)

    def pending_note_updates(self) -> dict[int, tuple[str, str]]:
        return {
            item_id: (self._note_draft_bases.get(item_id, ""), notes)
            for item_id, notes in self._note_drafts.items()
        }

    @property
    def loaded_notes(self) -> str:
        return self._loaded_notes

