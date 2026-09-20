from __future__ import annotations

from collections import OrderedDict

from PySide6.QtCore import QModelIndex, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QColor,
    QFont,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QTextDocument,
    QTextOption,
)
from .asset_grid_transition_renderer import AssetGridTransitionRenderer
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

from .constants import TYPE_LABELS
from .asset_text_layout import (
    plain_text_layout,
    plain_text_layout_signature,
    plain_text_layout_source,
    plain_text_wrap_mode,
)
from .item_models import AssetItemModel, format_local_timestamp
from .ui_primitives import dark_theme_active, lucide_icon


class AssetGridDelegate(QStyledItemDelegate):
    card_height = 252

    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.favorite_on = lucide_icon("star", "#f4a100", 18, "#f4a100").pixmap(18, 18)
        self.favorite_off = lucide_icon("star", "#f4a100", 18).pixmap(18, 18)
        self._markdown_documents: OrderedDict[tuple[object, ...], QTextDocument] = OrderedDict()
        self._transition_renderer = AssetGridTransitionRenderer(self)
        self._transition_preview_caches = self._transition_renderer.preview_caches
        self._transition_layout_signatures = self._transition_renderer.layout_signatures

    def clear_transition_caches(self) -> None:
        self._transition_renderer.clear()

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
        return plain_text_wrap_mode(content)

    @staticmethod
    def _plain_text_layout_source(
        content: str,
    ) -> tuple[str, tuple[int, ...]]:
        return plain_text_layout_source(content)

    @staticmethod
    def _plain_text_layout(
        content: str,
        width: int,
        height: int,
        font: QFont,
    ):
        return plain_text_layout(content, width, height, font)

    @staticmethod
    def _plain_text_layout_signature(
        content: str,
        width: int,
        height: int,
        font: QFont,
    ) -> tuple[tuple[int, int], ...]:
        return plain_text_layout_signature(content, width, height, font)

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

    def transition_layout_signature(
        self,
        index: QModelIndex,
        cell_size: QSize,
    ) -> tuple[object, ...]:
        return self._transition_renderer.layout_signature(index, cell_size)

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

    def render_transition_preview(
        self,
        index: QModelIndex,
        cell_size: QSize,
        layout_signature: tuple[object, ...] | None = None,
    ) -> QPixmap | None:
        return self._transition_renderer.render_preview(
            index,
            cell_size,
            layout_signature,
        )

    @staticmethod
    def transition_image_target(preview: QRect, pixmap: QPixmap) -> QRectF:
        return AssetGridTransitionRenderer.image_target(preview, pixmap)

    @staticmethod
    def transition_preview_clip(preview: QRect, kind: str) -> QRect:
        return AssetGridTransitionRenderer.preview_clip(preview, kind)

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

