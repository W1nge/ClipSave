from __future__ import annotations

import re
from collections import OrderedDict

from PySide6.QtCore import QModelIndex, QPointF, QRect, QRectF, QSize, QSizeF, Qt
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QTextDocument,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

from .constants import TYPE_LABELS
from .item_models import AssetItemModel, format_local_timestamp
from .ui_primitives import dark_theme_active, lucide_icon


_MACHINE_TEXT_SPAN_RE = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9+.-]*://|www\.)"
    r"[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+"
    r"|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    r"|(?:[A-Za-z]:[\\/]|\\\\)[^\s\r\n]+"
    r"|(?:\.{0,2}/|~/|/)?(?:[A-Za-z0-9._-]+[\\/])+[A-Za-z0-9._-]+"
    r"|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|[0-9a-fA-F]{20,}"
    r"|[A-Za-z0-9_+/=-]{20,}"
)


class AssetGridDelegate(QStyledItemDelegate):
    card_height = 252

    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.favorite_on = lucide_icon("star", "#f4a100", 18, "#f4a100").pixmap(18, 18)
        self.favorite_off = lucide_icon("star", "#f4a100", 18).pixmap(18, 18)
        self._markdown_documents: OrderedDict[tuple[object, ...], QTextDocument] = OrderedDict()
        self._transition_preview_caches: OrderedDict[
            tuple[object, ...], QPixmap
        ] = OrderedDict()
        self._transition_layout_signatures: OrderedDict[
            tuple[object, ...], tuple[object, ...]
        ] = OrderedDict()

    def clear_transition_caches(self) -> None:
        self._transition_preview_caches.clear()
        self._transition_layout_signatures.clear()

    def sizeHint(self, option, index) -> QSize:
        return self.view.gridSize()

    @staticmethod
    def card_rect(rect: QRect) -> QRect:
        return rect.adjusted(6, 6, -6, -6)

    def preview_rect(self, rect: QRect) -> QRect:
        return self.card_rect(rect).adjusted(10, 42, -10, -10)

    def kind_rect(self, rect: QRect) -> QRect:
        card = self.card_rect(rect)
        return QRect(card.left() + 10, card.top() + 10, 90, 24)

    def time_rect(self, rect: QRect) -> QRect:
        card = self.card_rect(rect)
        return QRect(card.right() - 102, card.top() + 10, 54, 24)

    def favorite_rect(self, rect: QRect) -> QRect:
        card = self.card_rect(rect)
        return QRect(card.right() - 31, card.top() + 9, 24, 24)

    def _markdown_document(
        self,
        content: str,
        width: int,
        dark: bool,
        font: QFont,
    ) -> QTextDocument:
        preview_content = content[:2000]
        key = (preview_content, width, dark, font.toString())
        cached = self._markdown_documents.pop(key, None)
        if cached is not None:
            self._markdown_documents[key] = cached
            return cached
        document = QTextDocument()
        document.setDocumentMargin(0)
        document.setDefaultFont(font)
        document.setMarkdown(preview_content)
        document.setTextWidth(max(1, width))
        self._markdown_documents[key] = document
        while len(self._markdown_documents) > 64:
            self._markdown_documents.popitem(last=False)
        return document

    def _draw_markdown_preview(
        self,
        painter: QPainter,
        rect: QRect,
        content: str,
        dark: bool,
        font: QFont,
    ) -> None:
        document = self._markdown_document(content, rect.width(), dark, font)
        painter.save()
        painter.setClipRect(rect)
        painter.translate(rect.topLeft())
        context = QAbstractTextDocumentLayout.PaintContext()
        context.clip = QRectF(0, 0, rect.width(), rect.height())
        context.palette.setColor(
            QPalette.ColorRole.Text,
            QColor("#dedede" if dark else "#354052"),
        )
        document.documentLayout().draw(painter, context)
        painter.restore()

    def paint(self, painter, option, index) -> None:
        if getattr(self.view, "_sidebar_transition_active", False):
            return
        self.paint_transition_card(painter, option, index)

    @staticmethod
    def _preview_background_color(record, dark: bool) -> QColor:
        if dark:
            return QColor("#303030" if record["kind"] == "image" else "#262626")
        return QColor("#edf1f7") if record["kind"] == "image" else QColor("#f7f9fc")

    def _paint_preview_background(
        self,
        painter: QPainter,
        preview: QRect,
        record,
        dark: bool,
    ) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._preview_background_color(record, dark))
        painter.drawRoundedRect(preview, 5, 5)

    @staticmethod
    def _plain_text_wrap_mode(content: str) -> QTextOption.WrapMode:
        value = content.strip()
        if not value:
            return QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere
        machine_text = (
            re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*://\S+", value)
            or re.fullmatch(r"www\.\S+", value, re.IGNORECASE)
            or re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value)
            or re.fullmatch(r"(?:[A-Za-z]:[\\/]|\\\\|/|\./|\.\./|~/).+", value)
            or re.fullmatch(r"\S*[\\/]\S*", value)
            or re.fullmatch(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                value,
            )
            or re.fullmatch(r"[0-9a-fA-F]{20,}", value)
            or re.fullmatch(r"[A-Za-z0-9_+/=-]{20,}", value)
            or re.fullmatch(
                r"(?:[A-Za-z0-9-]+\.)+[A-Za-z0-9-]{2,}(?:/\S*)?",
                value,
            )
        )
        if machine_text:
            return QTextOption.WrapMode.WrapAnywhere
        return QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere

    @staticmethod
    def _plain_text_layout_source(
        content: str,
    ) -> tuple[str, tuple[int, ...]]:
        source = content[:330]
        if (
            AssetGridDelegate._plain_text_wrap_mode(source)
            == QTextOption.WrapMode.WrapAnywhere
        ):
            return source, tuple(range(len(source) + 1))
        machine_positions = [False] * len(source)
        for match in _MACHINE_TEXT_SPAN_RE.finditer(source):
            machine_positions[match.start() : match.end()] = [True] * (
                match.end() - match.start()
            )
        if not any(machine_positions):
            return source, tuple(range(len(source) + 1))
        transformed: list[str] = []
        original_boundaries = [0]
        for index, character in enumerate(source):
            transformed.append(character)
            original_boundaries.append(index + 1)
            if machine_positions[index]:
                transformed.append("\u200b")
                original_boundaries.append(index + 1)
        return "".join(transformed), tuple(original_boundaries)

    @staticmethod
    def _plain_text_layout(
        content: str,
        width: int,
        height: int,
        font: QFont,
    ) -> tuple[list[QTextLayout], tuple[tuple[int, int], ...]]:
        source, original_boundaries = (
            AssetGridDelegate._plain_text_layout_source(content)
        )
        layouts: list[QTextLayout] = []
        lines: list[tuple[int, int]] = []
        transformed_offset = 0
        y = 0.0
        segments = source.splitlines(keepends=True) or [source]
        for segment in segments:
            if y >= max(1, height):
                break
            newline_length = len(segment) - len(segment.rstrip("\r\n"))
            paragraph = (
                segment[:-newline_length] if newline_length else segment
            )
            layout = QTextLayout(paragraph, font)
            option = QTextOption()
            option.setWrapMode(
                AssetGridDelegate._plain_text_wrap_mode(content)
            )
            layout.setTextOption(option)
            layout.beginLayout()
            paragraph_line_indexes: list[int] = []
            completed = False
            while y < max(1, height):
                line = layout.createLine()
                if not line.isValid():
                    completed = True
                    break
                line.setLineWidth(max(1, width))
                line.setPosition(QPointF(0.0, y))
                transformed_start = (
                    transformed_offset + line.textStart()
                )
                transformed_end = transformed_start + line.textLength()
                original_start = original_boundaries[transformed_start]
                original_end = original_boundaries[transformed_end]
                paragraph_line_indexes.append(len(lines))
                lines.append(
                    (original_start, original_end - original_start)
                )
                y += line.height()
            layout.endLayout()
            layouts.append(layout)
            if not paragraph:
                y += QFontMetricsF(font).height()
                completed = True
            if completed and newline_length:
                newline_start = transformed_offset + len(paragraph)
                newline_end = newline_start + newline_length
                original_newline_length = (
                    original_boundaries[newline_end]
                    - original_boundaries[newline_start]
                )
                if paragraph_line_indexes:
                    line_index = paragraph_line_indexes[-1]
                    start, length = lines[line_index]
                    lines[line_index] = (
                        start,
                        length + original_newline_length,
                    )
                else:
                    lines.append(
                        (
                            original_boundaries[newline_start],
                            original_newline_length,
                        )
                    )
            transformed_offset += len(segment)
        return layouts, tuple(lines)

    @staticmethod
    def _plain_text_layout_signature(
        content: str,
        width: int,
        height: int,
        font: QFont,
    ) -> tuple[tuple[int, int], ...]:
        _layouts, lines = AssetGridDelegate._plain_text_layout(
            content,
            width,
            height,
            font,
        )
        return lines

    @staticmethod
    def _draw_plain_text_preview(
        painter: QPainter,
        rect: QRect,
        content: str,
        font: QFont,
    ) -> None:
        layouts, _lines = AssetGridDelegate._plain_text_layout(
            content,
            max(1, rect.width()),
            max(1, rect.height()),
            font,
        )
        painter.save()
        painter.setClipRect(rect)
        for layout in layouts:
            layout.draw(painter, QPointF(rect.left(), rect.top()))
        painter.restore()

    def _markdown_layout_signature(
        self,
        content: str,
        width: int,
        height: int,
        dark: bool,
        font: QFont,
    ) -> tuple[tuple[int, int], ...]:
        document = self._markdown_document(content, width, dark, font)
        document.documentLayout().documentSize()
        lines: list[tuple[int, int]] = []
        block = document.begin()
        while block.isValid():
            layout = block.layout()
            block_top = layout.position().y()
            for line_index in range(layout.lineCount()):
                line = layout.lineAt(line_index)
                if block_top + line.y() >= height:
                    return tuple(lines)
                lines.append(
                    (
                        block.position() + line.textStart(),
                        line.textLength(),
                    )
                )
            block = block.next()
        return tuple(lines)

    def transition_layout_signature(
        self,
        index: QModelIndex,
        cell_size: QSize,
    ) -> tuple[object, ...]:
        record = index.data(AssetItemModel.ItemRole)
        if record is None or cell_size.isEmpty():
            return ("empty",)
        preview = self.preview_rect(
            QRect(0, 0, cell_size.width(), cell_size.height())
        )
        content_rect = preview.adjusted(10, 9, -10, -9)
        kind = str(record["kind"])
        if kind == "image":
            return (
                "image",
                max(1, content_rect.width()),
                max(1, content_rect.height()),
            )
        content = str(record["content"] or "").strip() or str(record["title"])
        dark = dark_theme_active()
        font = self.view.font()
        cache_key = (
            kind,
            content[:2000] if kind == "markdown" else content[:330],
            max(1, content_rect.width()),
            max(1, content_rect.height()),
            dark,
            font.toString(),
        )
        cached = self._transition_layout_signatures.pop(cache_key, None)
        if cached is not None:
            self._transition_layout_signatures[cache_key] = cached
            return cached
        if kind == "markdown":
            signature = (
                "markdown",
                self._markdown_layout_signature(
                    content,
                    max(1, content_rect.width()),
                    max(1, content_rect.height()),
                    dark,
                    font,
                ),
            )
        else:
            signature = (
                "text",
                self._plain_text_layout_signature(
                    content,
                    max(1, content_rect.width()),
                    max(1, content_rect.height()),
                    font,
                ),
            )
        self._transition_layout_signatures[cache_key] = signature
        while len(self._transition_layout_signatures) > 2048:
            self._transition_layout_signatures.popitem(last=False)
        return signature

    def _paint_preview_content(
        self,
        painter: QPainter,
        preview: QRect,
        index: QModelIndex,
        record,
        dark: bool,
        font: QFont,
    ) -> None:
        painter.setFont(font)
        path = record["path"] if record["kind"] == "image" else None
        if path and self.view.preview_loading_enabled and self.view.isVisible():
            try:
                content_hash = record["content_hash"]
            except (KeyError, IndexError):
                content_hash = None
            pixmap = self.view.thumbnail_for_index(index, path, content_hash)
            if pixmap is not None and not pixmap.isNull():
                scaled = pixmap.scaled(
                    preview.size() - QSize(12, 12),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                target = QRect(0, 0, scaled.width(), scaled.height())
                target.moveCenter(preview.center())
                painter.drawPixmap(target, scaled)
        elif record["kind"] != "image":
            content = str(record["content"] or "").strip() or str(record["title"])
            if record["kind"] == "markdown":
                self._draw_markdown_preview(
                    painter,
                    preview.adjusted(10, 9, -10, -9),
                    content,
                    dark,
                    font,
                )
            else:
                painter.setPen(QColor("#dedede" if dark else "#354052"))
                self._draw_plain_text_preview(
                    painter,
                    preview.adjusted(10, 9, -10, -9),
                    content,
                    font,
                )

    @staticmethod
    def _transition_record_signature(record) -> tuple[object, ...]:
        try:
            content_hash = record["content_hash"]
        except (KeyError, IndexError):
            content_hash = None
        return (
            int(record["id"]),
            str(record["kind"]),
            hash(str(record["title"])),
            hash(str(record["content"] or "")),
            str(record["path"] or ""),
            content_hash,
        )

    def render_transition_preview(
        self,
        index: QModelIndex,
        cell_size: QSize,
        layout_signature: tuple[object, ...] | None = None,
    ) -> QPixmap | None:
        record = index.data(AssetItemModel.ItemRole)
        if record is None or cell_size.isEmpty():
            return None
        cell = QRect(0, 0, cell_size.width(), cell_size.height())
        preview = self.preview_rect(cell)
        if preview.isEmpty():
            return None
        device_pixel_ratio = max(1.0, float(self.view.devicePixelRatioF()))
        dark = dark_theme_active()
        font = self.view.font()
        if layout_signature is None:
            layout_signature = self.transition_layout_signature(index, cell_size)
        if record["kind"] == "image":
            cache_key = (
                "transition-image",
                self._transition_record_signature(record),
                layout_signature,
                round(device_pixel_ratio, 3),
            )
            cached = self._transition_preview_caches.pop(cache_key, None)
            if cached is not None:
                self._transition_preview_caches[cache_key] = cached
                return cached
            path = record["path"]
            if not path:
                return None
            try:
                content_hash = record["content_hash"]
            except (KeyError, IndexError):
                content_hash = None
            source = self.view.thumbnail_for_index(index, path, content_hash)
            if source is None or source.isNull():
                return None
            logical_size = preview.size() - QSize(12, 12)
            physical_size = QSize(
                max(1, round(logical_size.width() * device_pixel_ratio)),
                max(1, round(logical_size.height() * device_pixel_ratio)),
            )
            pixmap = source.scaled(
                physical_size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            pixmap.setDevicePixelRatio(device_pixel_ratio)
            self._transition_preview_caches[cache_key] = pixmap
            while len(self._transition_preview_caches) > 192:
                self._transition_preview_caches.popitem(last=False)
            return pixmap
        cache_key = (
            self._transition_record_signature(record),
            layout_signature,
            dark,
            font.toString(),
            round(device_pixel_ratio, 3),
        )
        cached = self._transition_preview_caches.pop(cache_key, None)
        if cached is not None:
            self._transition_preview_caches[cache_key] = cached
            return cached
        pixmap = QPixmap(
            max(1, round(preview.width() * device_pixel_ratio)),
            max(1, round(preview.height() * device_pixel_ratio)),
        )
        pixmap.setDevicePixelRatio(device_pixel_ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._paint_preview_content(
            painter,
            QRect(0, 0, preview.width(), preview.height()),
            index,
            record,
            dark,
            font,
        )
        painter.end()
        self._transition_preview_caches[cache_key] = pixmap
        while len(self._transition_preview_caches) > 192:
            self._transition_preview_caches.popitem(last=False)
        return pixmap

    @staticmethod
    def transition_image_target(preview: QRect, pixmap: QPixmap) -> QRectF:
        available = QSizeF(
            max(1, preview.width() - 12),
            max(1, preview.height() - 12),
        )
        source_size = pixmap.deviceIndependentSize()
        if source_size.isEmpty():
            return QRectF()
        target_size = source_size.scaled(
            available,
            Qt.AspectRatioMode.KeepAspectRatio,
        )
        return QRectF(
            preview.center().x() - target_size.width() / 2.0,
            preview.center().y() - target_size.height() / 2.0,
            target_size.width(),
            target_size.height(),
        )

    @staticmethod
    def transition_preview_clip(preview: QRect, kind: str) -> QRect:
        if kind == "image":
            return preview
        return preview.adjusted(10, 9, -10, -9)

    def paint_transition_card(
        self,
        painter,
        option,
        index,
        preview_cache: QPixmap | None = None,
    ) -> None:
        record = index.data(AssetItemModel.ItemRole)
        if record is None:
            return
        card = self.card_rect(option.rect)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(option.font)
        dark = dark_theme_active()
        painter.setPen(QPen(QColor("#4da3ff") if selected else QColor("#4a4a4a" if dark else "#dfe4eb"), 1))
        painter.setBrush(QColor("#26384d") if selected and dark else QColor("#eef4ff") if selected else QColor("#292929" if dark else "#ffffff"))
        painter.drawRoundedRect(card, 6, 6)

        muted = QColor("#a7adb7" if dark else "#7a8699")
        kind = TYPE_LABELS.get(record["kind"], record["kind"])
        painter.setPen(muted)
        painter.drawText(
            self.kind_rect(option.rect),
            Qt.AlignmentFlag.AlignVCenter,
            kind,
        )
        created_at = format_local_timestamp(record["created_at"])
        time_text = created_at[11:16] if len(created_at) >= 16 else ""
        painter.drawText(
            self.time_rect(option.rect),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            time_text,
        )
        favorite = self.favorite_on if record["favorite"] else self.favorite_off
        favorite_target = self.favorite_rect(option.rect)
        painter.drawPixmap(
            favorite_target.left() + 3,
            favorite_target.top() + 3,
            favorite,
        )

        preview = self.preview_rect(option.rect)
        self._paint_preview_background(painter, preview, record, dark)
        if preview_cache is not None and not preview_cache.isNull():
            painter.save()
            painter.setClipRect(
                self.transition_preview_clip(preview, record["kind"])
            )
            if record["kind"] == "image":
                target = self.transition_image_target(preview, preview_cache)
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                painter.drawPixmap(
                    target,
                    preview_cache,
                    QRectF(preview_cache.rect()),
                )
            else:
                painter.drawPixmap(preview.topLeft(), preview_cache)
            painter.restore()
        else:
            self._paint_preview_content(
                painter,
                preview,
                index,
                record,
                dark,
                option.font,
            )

        painter.restore()

