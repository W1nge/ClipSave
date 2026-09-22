from __future__ import annotations

from collections import OrderedDict

from PySide6.QtCore import QModelIndex, QRect, QRectF, QSize, QSizeF, Qt
from PySide6.QtGui import QFont, QPainter, QPixmap

from .asset_text_layout import plain_text_layout_signature
from .item_models import AssetItemModel
from .ui_primitives import dark_theme_active


class AssetGridTransitionRenderer:
    """Own transition-only preview layout signatures and pixmap caches."""

    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.view = delegate.view
        self.preview_caches: OrderedDict[tuple[object, ...], QPixmap] = OrderedDict()
        self.layout_signatures: OrderedDict[
            tuple[object, ...], tuple[object, ...]
        ] = OrderedDict()

    def clear(self) -> None:
        self.preview_caches.clear()
        self.layout_signatures.clear()

    def _markdown_layout_signature(
        self,
        content: str,
        width: int,
        height: int,
        dark: bool,
        font: QFont,
    ) -> tuple[tuple[int, int], ...]:
        document = self.delegate._markdown_document(content, width, dark, font)
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

    def layout_signature(
        self,
        index: QModelIndex,
        cell_size: QSize,
    ) -> tuple[object, ...]:
        record = index.data(AssetItemModel.ItemRole)
        if record is None or cell_size.isEmpty():
            return ("empty",)
        preview = self.delegate.preview_rect(
            QRect(0, 0, cell_size.width(), cell_size.height())
        )
        content_rect = preview
        kind = str(record["kind"])
        if kind == "image":
            return (
                "image",
                max(1, content_rect.width()),
                max(1, content_rect.height()),
            )
        content = str(record["content"] or "").strip() or str(record["title"])
        dark = dark_theme_active() and not bool(record["favorite"])
        font = self.view.font()
        cache_key = (
            kind,
            content[:2000] if kind == "markdown" else content[:330],
            max(1, content_rect.width()),
            max(1, content_rect.height()),
            dark,
            font.toString(),
        )
        cached = self.layout_signatures.pop(cache_key, None)
        if cached is not None:
            self.layout_signatures[cache_key] = cached
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
                plain_text_layout_signature(
                    content,
                    max(1, content_rect.width()),
                    max(1, content_rect.height()),
                    font,
                ),
            )
        self.layout_signatures[cache_key] = signature
        while len(self.layout_signatures) > 2048:
            self.layout_signatures.popitem(last=False)
        return signature

    @staticmethod
    def _record_signature(record) -> tuple[object, ...]:
        try:
            content_hash = record["content_hash"]
        except (KeyError, IndexError):
            content_hash = None
        return (
            int(record["id"]),
            str(record["kind"]),
            bool(record["favorite"]),
            hash(str(record["title"])),
            hash(str(record["content"] or "")),
            str(record["path"] or ""),
            content_hash,
        )

    def render_preview(
        self,
        index: QModelIndex,
        cell_size: QSize,
        layout_signature: tuple[object, ...] | None = None,
    ) -> QPixmap | None:
        record = index.data(AssetItemModel.ItemRole)
        if record is None or cell_size.isEmpty():
            return None
        cell = QRect(0, 0, cell_size.width(), cell_size.height())
        preview = self.delegate.preview_rect(cell)
        if preview.isEmpty():
            return None
        device_pixel_ratio = max(1.0, float(self.view.devicePixelRatioF()))
        # Favorite cards use yellow paper with dark text.  Transition previews
        # are cached independently from the live delegate, so carrying the
        # application's dark theme through here briefly painted their body
        # text white while the sidebar was moving.
        dark = dark_theme_active() and not bool(record["favorite"])
        font = self.view.font()
        if layout_signature is None:
            layout_signature = self.layout_signature(index, cell_size)
        if record["kind"] == "image":
            cache_key = (
                "transition-image",
                self._record_signature(record),
                layout_signature,
                round(device_pixel_ratio, 3),
            )
            cached = self.preview_caches.pop(cache_key, None)
            if cached is not None:
                self.preview_caches[cache_key] = cached
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
            logical_size = preview.size()
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
            self._remember_preview(cache_key, pixmap)
            return pixmap
        cache_key = (
            self._record_signature(record),
            layout_signature,
            dark,
            font.toString(),
            round(device_pixel_ratio, 3),
        )
        cached = self.preview_caches.pop(cache_key, None)
        if cached is not None:
            self.preview_caches[cache_key] = cached
            return cached
        pixmap = QPixmap(
            max(1, round(preview.width() * device_pixel_ratio)),
            max(1, round(preview.height() * device_pixel_ratio)),
        )
        pixmap.setDevicePixelRatio(device_pixel_ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.delegate._paint_preview_content(
            painter,
            QRect(0, 0, preview.width(), preview.height()),
            index,
            record,
            dark,
            font,
        )
        painter.end()
        self._remember_preview(cache_key, pixmap)
        return pixmap

    def _remember_preview(self, key: tuple[object, ...], pixmap: QPixmap) -> None:
        self.preview_caches[key] = pixmap
        while len(self.preview_caches) > 192:
            self.preview_caches.popitem(last=False)

    @staticmethod
    def image_target(preview: QRect, pixmap: QPixmap) -> QRectF:
        available = QSizeF(
            max(1, preview.width()),
            max(1, preview.height()),
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
    def preview_clip(preview: QRect, kind: str) -> QRect:
        return preview
