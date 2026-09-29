from __future__ import annotations

import datetime as dt
import math
from collections import OrderedDict

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import (
    QAbstractTextDocumentLayout, QColor, QFont, QPainter,
    QPainterPath, QPalette, QPen, QPixmap, QPolygonF, QTextDocument,
    QTextOption, QTransform,
)
from PySide6.QtWidgets import QStyle, QStyleOptionViewItem, QStyledItemDelegate

from .asset_grid_transition_renderer import AssetGridTransitionRenderer
from .asset_text_layout import (
    plain_text_layout, plain_text_layout_signature, plain_text_layout_source,
    plain_text_wrap_mode,
)
from .card_markdown import build_card_markdown_document
from .item_models import AssetItemModel
from .ui_primitives import dark_theme_active


def format_card_timestamp(value, now: dt.datetime | None = None) -> str:
    text = str(value or "")
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text[:16].replace("T", " ")
    reference = now or dt.datetime.now().astimezone()
    if reference.tzinfo is None:
        reference = reference.astimezone()
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    parsed = parsed.astimezone(reference.tzinfo)
    if parsed.date() == reference.date():
        return parsed.strftime("%H:%M")
    if parsed.year == reference.year:
        return parsed.strftime("%m/%d")
    return parsed.strftime("%Y/%m/%d")


def _clip_polygon(polygon, midpoint: QPointF, normal: QPointF, keep_positive: bool) -> QPolygonF:
    def distance(point: QPointF) -> float:
        return ((point.x() - midpoint.x()) * normal.x()
                + (point.y() - midpoint.y()) * normal.y())

    def inside(point: QPointF) -> bool:
        value = distance(point)
        return value >= -0.01 if keep_positive else value <= 0.01

    clipped: list[QPointF] = []
    for index, current in enumerate(polygon):
        previous = polygon[index - 1]
        current_inside, previous_inside = inside(current), inside(previous)
        if current_inside != previous_inside:
            a, b = distance(previous), distance(current)
            ratio = a / (a - b) if abs(a - b) > 1e-6 else 0.0
            clipped.append(QPointF(
                previous.x() + (current.x() - previous.x()) * ratio,
                previous.y() + (current.y() - previous.y()) * ratio,
            ))
        if current_inside:
            clipped.append(current)
    return QPolygonF(clipped)


def _polygon_path(polygon: QPolygonF) -> QPainterPath:
    path = QPainterPath()
    if len(polygon):
        path.addPolygon(polygon)
        path.closeSubpath()
    return path


class AssetGridDelegate(QStyledItemDelegate):
    CARD_ASPECT = 183 / 141
    CARD_RADIUS = 7.0
    CORNER_SIZE = 24
    CORNER_HIT_SIZE = 40
    CARD_CACHE_LIMIT = 128
    CARD_CACHE_BYTES = 96 * 1024 * 1024

    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self._markdown_documents: OrderedDict[tuple[object, ...], QTextDocument] = OrderedDict()
        self._transition_renderer = AssetGridTransitionRenderer(self)
        self._transition_preview_caches = self._transition_renderer.preview_caches
        self._transition_layout_signatures = self._transition_renderer.layout_signatures
        self._card_caches: OrderedDict[tuple[object, ...], QPixmap] = OrderedDict()
        self._card_cache_bytes = 0
        self._interactive_resize_previews: OrderedDict[tuple[object, ...], QPixmap] = OrderedDict()

    def clear_interactive_resize_caches(self) -> None:
        self._interactive_resize_previews.clear()

    def clear_transition_caches(self) -> None:
        self._transition_renderer.clear()
        self._markdown_documents.clear()
        self._card_caches.clear()
        self._card_cache_bytes = 0
        self.clear_interactive_resize_caches()

    def clear_markdown_documents(self) -> None:
        self._markdown_documents.clear()
        self._card_caches.clear()
        self.clear_interactive_resize_caches()
        self._card_cache_bytes = 0

    def sizeHint(self, option, index) -> QSize:
        return self.view.gridSize()

    @staticmethod
    def card_rect(rect: QRect) -> QRect:
        return rect.adjusted(6, 6, -6, -6)

    def preview_rect(self, rect: QRect) -> QRect:
        return self.card_rect(rect).adjusted(12, 54, -12, -12)

    def time_rect(self, rect: QRect) -> QRect:
        card = self.card_rect(rect)
        left_inset = max(12, round(card.width() * 0.08))
        return QRect(
            card.left() + left_inset,
            card.top() + 9,
            max(1, card.width() - left_inset - 36),
            24,
        ).translated(-1, 0)

    def favorite_rect(self, rect: QRect) -> QRect:
        card = self.card_rect(rect)
        return QRect(card.right() - self.CORNER_HIT_SIZE + 1, card.top(),
                     self.CORNER_HIT_SIZE, self.CORNER_HIT_SIZE)

    @staticmethod
    def paper_corner(card: QRect) -> QPointF:
        # QRect.right() is the centre of its final pixel; painted QRectF paths
        # end one device coordinate later.  The fold must use that true outer
        # corner or its curve sits one pixel inside the card's rounded edge.
        return QPointF(card.right() + 1.0, card.top())

    def _markdown_document(self, content: str, width: int, dark: bool, font: QFont) -> QTextDocument:
        preview_content = content[:2000]
        key = (preview_content, width, dark, font.toString())
        cached = self._markdown_documents.pop(key, None)
        if cached is not None:
            self._markdown_documents[key] = cached
            return cached
        document = build_card_markdown_document(preview_content, dark, font)
        document.setTextWidth(max(1, width))
        self._markdown_documents[key] = document
        while len(self._markdown_documents) > 64:
            self._markdown_documents.popitem(last=False)
        return document

    def _draw_markdown_preview(self, painter, rect, content, dark, font, *, clip=True) -> None:
        document = self._markdown_document(content, rect.width(), dark, font)
        painter.save()
        if clip:
            painter.setClipRect(rect, Qt.ClipOperation.IntersectClip)
        painter.translate(rect.topLeft())
        context = QAbstractTextDocumentLayout.PaintContext()
        if clip:
            context.clip = QRectF(0, 0, rect.width(), rect.height())
        context.palette.setColor(QPalette.ColorRole.Text,
                                 QColor("#e5e7eb" if dark else "#303947"))
        document.documentLayout().draw(painter, context)
        painter.restore()

    @staticmethod
    def _plain_text_wrap_mode(content: str) -> QTextOption.WrapMode:
        return plain_text_wrap_mode(content)

    @staticmethod
    def _plain_text_layout_source(content: str):
        return plain_text_layout_source(content)

    @staticmethod
    def _plain_text_layout(content: str, width: int, height: int, font: QFont):
        return plain_text_layout(content, width, height, font)

    @staticmethod
    def _plain_text_layout_signature(content: str, width: int, height: int, font: QFont):
        return plain_text_layout_signature(content, width, height, font)

    @staticmethod
    def _draw_plain_text_preview(painter, rect, content, font, *, clip=True) -> None:
        layouts, _lines = AssetGridDelegate._plain_text_layout(
            content, max(1, rect.width()), max(1, rect.height()), font)
        painter.save()
        if clip:
            painter.setClipRect(rect, Qt.ClipOperation.IntersectClip)
        for layout in layouts:
            layout.draw(painter, QPointF(rect.left(), rect.top()))
        painter.restore()

    def transition_layout_signature(self, index, cell_size):
        return self._transition_renderer.layout_signature(index, cell_size)

    def _paint_preview_content(self, painter, preview, index, record, dark, font, *, clip=True) -> None:
        painter.setFont(font)
        path = record["path"] if record["kind"] == "image" else None
        if path and self.view.preview_loading_enabled and self.view.isVisible():
            try:
                content_hash = record["content_hash"]
            except (KeyError, IndexError):
                content_hash = None
            pixmap = self.view.thumbnail_for_index(index, path, content_hash)
            if pixmap is not None and not pixmap.isNull():
                scaled = pixmap.scaled(preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation)
                target = QRect(0, 0, scaled.width(), scaled.height())
                target.moveCenter(preview.center())
                painter.drawPixmap(target, scaled)
        elif record["kind"] != "image":
            content = str(record["content"] or "").strip() or str(record["title"])
            painter.setPen(QColor("#e5e7eb" if dark else "#303947"))
            if record["kind"] == "markdown":
                self._draw_markdown_preview(painter, preview, content, dark, font, clip=clip)
            else:
                self._draw_plain_text_preview(painter, preview, content, font, clip=clip)

    def render_transition_preview(self, index, cell_size, layout_signature=None):
        return self._transition_renderer.render_preview(index, cell_size, layout_signature)

    def transition_preview_covers(self, cache, cell_size) -> bool:
        return self._transition_renderer.preview_covers(cache, cell_size)

    @staticmethod
    def transition_image_target(preview: QRect, pixmap: QPixmap) -> QRectF:
        return AssetGridTransitionRenderer.image_target(preview, pixmap)

    @staticmethod
    def transition_preview_clip(preview: QRect, kind: str) -> QRect:
        return AssetGridTransitionRenderer.preview_clip(preview, kind)

    @staticmethod
    def _paper_colors(favorite: bool, dark: bool):
        navy = QColor("#292929") if dark else QColor("#ffffff")
        yellow = QColor(247, 195, 63) if dark else QColor(248, 196, 63)
        border = QColor(232, 236, 242, 62) if dark else QColor(55, 63, 74, 46)
        return (yellow, navy, border) if favorite else (navy, yellow, border)

    @staticmethod
    def _card_shape_path(paper_rect: QRectF) -> QPainterPath:
        path = QPainterPath()
        radius = AssetGridDelegate.CARD_RADIUS
        path.addRoundedRect(paper_rect, radius, radius)
        return path

    @staticmethod
    def _paint_card_outline(painter, paper_rect: QRectF, border: QColor) -> None:
        # Center a one-pixel stroke on half-pixel inset geometry so all four
        # edges rasterize inside the sheet.  This keeps the right/bottom edge
        # intact without building an OddEven compound path; repeated compound
        # rounded paths can corrupt Qt 6 native path storage on Python 3.11/12.
        painter.save()
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        radius = max(0.0, AssetGridDelegate.CARD_RADIUS - 0.5)
        painter.drawRoundedRect(
            paper_rect.adjusted(0.5, 0.5, -0.5, -0.5),
            radius,
            radius,
        )
        painter.restore()

    def _paint_sheet(
        self, painter, option, index, record, fill, border, preview_cache=None,
        paint_outline: bool = True,
        paint_content: bool = True,
    ) -> None:
        card = self.card_rect(option.rect)
        paper_rect = QRectF(card)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawPath(self._card_shape_path(paper_rect))
        # Keep the stroke inside the card so its right and bottom edges cannot
        # be clipped by the item's half-open raster bounds.
        if paint_outline:
            self._paint_card_outline(painter, paper_rect, border)
        if paint_content:
            self._paint_sheet_content(
                painter, option, index, record, fill, preview_cache,
            )

    def _paint_sheet_content(
        self, painter, option, index, record, fill, preview_cache=None,
        timestamp_text: str | None = None,
    ) -> None:
        card = self.card_rect(option.rect)
        dark = dark_theme_active()
        yellow_paper = isinstance(fill, QColor) and fill.red() > fill.blue() * 2
        painter.setFont(option.font)
        painter.setPen(QColor("#34404e" if yellow_paper else "#d9e1eb" if dark else "#45576a"))
        painter.drawText(self.time_rect(option.rect),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         timestamp_text if timestamp_text is not None
                         else format_card_timestamp(record["last_used_at"]))
        divider_y = card.top() + 41
        divider = (
            QColor(53, 62, 73, 48)
            if yellow_paper
            else QColor(142, 170, 200, 36)
            if dark
            else QColor(57, 76, 96, 34)
        )
        painter.setPen(QPen(divider, 1))
        painter.drawLine(card.left() + 12, divider_y, card.right() - 12, divider_y)
        preview = self.preview_rect(option.rect)
        if preview_cache is not None and not preview_cache.isNull():
            painter.save()
            painter.setClipRect(
                self.transition_preview_clip(preview, record["kind"]),
                Qt.ClipOperation.IntersectClip,
            )
            if record["kind"] == "image":
                painter.drawPixmap(self.transition_image_target(preview, preview_cache),
                                   preview_cache, QRectF(preview_cache.rect()))
            else:
                # Text is rendered with the view's font into the transition
                # cache.  Scaling that bitmap as the card moves briefly
                # changes the apparent font size between layout states.
                painter.drawPixmap(preview.topLeft(), preview_cache)
            painter.restore()
        else:
            self._paint_preview_content(
                painter,
                preview,
                index,
                record,
                False if yellow_paper else dark,
                option.font,
            )

    def cached_card_pixmap(self, option, index) -> QPixmap | None:
        record = index.data(AssetItemModel.ItemRole)
        if record is None or record["kind"] == "image":
            return None
        kind = str(record["kind"])
        content = str(record["content"] or "").strip() or str(record["title"])
        preview_source = content[:2000 if kind == "markdown" else 330]
        dpr = max(1.0, float(self.view.devicePixelRatioF()))
        key = (
            int(record["id"]), kind, preview_source,
            str(record["last_used_at"]), bool(record["favorite"]),
            option.rect.size().width(), option.rect.size().height(),
            option.font.toString(), dpr, dark_theme_active(),
            bool(getattr(self.view, "favorite_page_mode", False)),
            dt.date.today(),
        )
        cached = self._card_caches.get(key)
        if cached is not None:
            self._card_caches.move_to_end(key)
            return cached
        width = max(1, option.rect.width())
        height = max(1, option.rect.height())
        cached = QPixmap(max(1, round(width * dpr)), max(1, round(height * dpr)))
        cached.setDevicePixelRatio(dpr)
        cached.fill(Qt.GlobalColor.transparent)
        copy = QStyleOptionViewItem(option)
        copy.rect = QRect(0, 0, width, height)
        local = QPainter(cached)
        # Use the same preview raster in settled and moving cards. At a
        # fractional DPI, drawing glyphs directly into the full card instead
        # uses a different subpixel origin and flashes when the drag ends.
        preview = self.render_transition_preview(index, copy.rect.size())
        self.paint_transition_card(local, copy, index, preview)
        local.end()
        self._card_caches[key] = cached
        self._card_cache_bytes += cached.width() * cached.height() * 4
        while (
            len(self._card_caches) > self.CARD_CACHE_LIMIT
            or self._card_cache_bytes > self.CARD_CACHE_BYTES
        ):
            _old_key, old = self._card_caches.popitem(last=False)
            self._card_cache_bytes -= old.width() * old.height() * 4
        return cached

    def paint(self, painter, option, index) -> None:
        if getattr(self.view, "_sidebar_transition_active", False):
            return
        if getattr(self.view, "paper_peel_state", lambda _row: None)(index.row()) is not None:
            return
        if getattr(self.view, "_interactive_resize_active", False):
            record = index.data(AssetItemModel.ItemRole)
            if record is not None and record["kind"] != "image":
                # A full-card cache includes the exact cell dimensions. During
                # a live resize that would regenerate every visible text card
                # for nearly every mouse pixel. Reuse previews while their
                # wrapping is unchanged, but never freeze the drag's initial
                # line layout as the paper shrinks or grows underneath it.
                signature = self.transition_layout_signature(index, option.rect.size())
                key = (
                    self.view.model().generation, index.row(),
                    int(record["id"]), bool(record["favorite"]),
                    option.font.toString(),
                    round(self.view.devicePixelRatioF(), 3),
                    dark_theme_active(),
                    signature,
                )
                preview = self._interactive_resize_previews.get(key)
                if preview is None or not self.transition_preview_covers(preview, option.rect.size()):
                    preview = self.render_transition_preview(index, option.rect.size(), signature)
                    if preview is not None:
                        self._interactive_resize_previews[key] = preview
                        while len(self._interactive_resize_previews) > 96:
                            self._interactive_resize_previews.popitem(last=False)
                self.paint_transition_card(painter, option, index, preview)
                return
        cached = self.cached_card_pixmap(option, index)
        if cached is not None:
            painter.drawPixmap(option.rect.topLeft(), cached)
        else:
            self.paint_transition_card(painter, option, index)

    def paint_transition_content(
        self, painter, option, index, preview_cache=None,
        *, record=None, timestamp_text: str | None = None,
    ) -> None:
        if record is None:
            record = index.data(AssetItemModel.ItemRole)
        if record is None:
            return
        dark = dark_theme_active()
        front, _back, _border = self._paper_colors(bool(record["favorite"]), dark)
        self._paint_sheet_content(
            painter, option, index, record, front, preview_cache, timestamp_text,
        )

    def paint_transition_card(
        self, painter, option, index, preview_cache=None,
        *, paint_content: bool = True,
    ) -> None:
        record = index.data(AssetItemModel.ItemRole)
        if record is None:
            return
        dark = dark_theme_active()
        front, back, border = self._paper_colors(bool(record["favorite"]), dark)
        # A light outline disappears on yellow paper. Give its resting fold
        # a darker warm edge while keeping the paper back flat/opaque.
        fold_border = QColor(130, 87, 24, 140) if record["favorite"] else border
        state = getattr(self.view, "paper_peel_state", lambda _row: None)(index.row())
        if state is not None and paint_content and preview_cache is None and record["kind"] != "image":
            preview_cache = self.render_transition_preview(index, option.rect.size())
        card = self.card_rect(option.rect)
        single_sheet = bool(getattr(self.view, "favorite_page_mode", False))
        corner = self.paper_corner(card)
        cursor = (QPointF(option.rect.left() + state.current.x(),
                          option.rect.top() + state.current.y()) if state is not None
                  else QPointF(corner.x() - self.CORNER_SIZE,
                               corner.y() + self.CORNER_SIZE))
        normal = cursor - corner
        length = math.hypot(normal.x(), normal.y())
        if length < 1.0:
            normal, length = QPointF(-1.0, 1.0), math.sqrt(2.0)
        midpoint = QPointF((corner.x() + cursor.x()) / 2,
                           (corner.y() + cursor.y()) / 2)
        # QRect.right()/bottom() point at the final integer pixel, while a
        # QPainterPath boundary is geometric and excludes the half-open far
        # edge during rasterization.  Extend the clipping polygon to the next
        # device coordinate so the card's final right/bottom pixel is retained.
        clip_right = card.right() + 1.0
        clip_bottom = card.bottom() + 1.0
        bounds = [QPointF(card.left(), card.top()), QPointF(clip_right, card.top()),
                  QPointF(clip_right, clip_bottom), QPointF(card.left(), clip_bottom)]
        retained = _clip_polygon(bounds, midpoint, normal, True)
        removed = _clip_polygon(bounds, midpoint, normal, False)
        card_shape = self._card_shape_path(QRectF(card))
        # Do not pre-compute ``polygon.intersected(rounded_rect)`` here.
        # Qt 6 can return an OddEven path whose interior is inverted when the
        # card is on the first layout row (y == 0).  That erased the complete
        # header of every first-row card.  Sequential painter clips perform
        # the same intersection at rasterization time without corrupting the
        # path fill rule.
        retained_path = _polygon_path(retained)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not single_sheet:
            painter.save()
            painter.setClipPath(card_shape, Qt.ClipOperation.IntersectClip)
            # A complete opaque base prevents a translucent seam between
            # the two antialiased paper edges. At rest only its corner shows;
            # while peeling, reveal the same record in the next paper color.
            if state is None or not paint_content:
                painter.fillPath(card_shape, back)
            else:
                # Match the raster and text contrast of the sheet that will
                # remain after favorite toggles, including fractional DPI.
                back_preview = None
                if record["kind"] != "image":
                    back_preview = self._transition_renderer.render_preview(
                        index, option.rect.size(),
                        dark=dark and bool(record["favorite"]),
                    )
                self._paint_sheet(
                    painter, option, index, record, back, border, back_preview,
                    paint_outline=False,
                )
            painter.restore()
        painter.save()
        painter.setClipPath(card_shape, Qt.ClipOperation.IntersectClip)
        painter.setClipPath(retained_path, Qt.ClipOperation.IntersectClip)
        self._paint_sheet(
            painter, option, index, record, front, border, preview_cache,
            paint_outline=False, paint_content=paint_content,
        )
        painter.restore()

        # The stationary sheet's outline must sit beneath the lifted paper.
        if not single_sheet:
            self._paint_card_outline(painter, QRectF(card), border)
        else:
            painter.save()
            painter.setClipPath(card_shape, Qt.ClipOperation.IntersectClip)
            painter.setClipPath(retained_path, Qt.ClipOperation.IntersectClip)
            self._paint_card_outline(painter, QRectF(card), border)
            painter.restore()

        if len(removed) >= 3:
            nx, ny = normal.x() / length, normal.y() / length
            m11, m12 = 1 - 2 * nx * nx, -2 * nx * ny
            m21, m22 = -2 * nx * ny, 1 - 2 * ny * ny
            dx = midpoint.x() - (m11 * midpoint.x() + m21 * midpoint.y())
            dy = midpoint.y() - (m12 * midpoint.x() + m22 * midpoint.y())
            reflection = QTransform(m11, m12, 0, m21, m22, 0, dx, dy, 1)
            # Avoid QPainterPath boolean operations and mapped rounded paths.
            # Qt 6 can corrupt their native storage during repeated transition
            # rendering on Python 3.11/3.12.  Reflect the painter instead: the
            # original rounded card path supplies the exact source-corner
            # radius without manufacturing a derived path.
            reflected_path = _polygon_path(
                QPolygonF([reflection.map(point) for point in removed])
            )
            # The lifted sheet presents a flat, unprinted, opaque back.
            painter.save()
            departure = state.departure_progress if state is not None else 0.0
            if departure > 0.0:
                # Fade the detached sheet as one object, including its edge.
                # The resting card and its revealed content stay unchanged.
                center = reflection.map(QRectF(card).center())
                painter.setOpacity(painter.opacity() * (1.0 - departure))
                drift = math.hypot(card.width(), card.height()) * 0.075 * departure
                painter.translate(nx * drift, ny * drift)
                painter.translate(center)
                painter.rotate(-5.0 * departure)
                painter.scale(1.0 - 0.04 * departure, 1.0 - 0.04 * departure)
                painter.translate(-center)
            # During a peel the reflected sheet can extend past the original
            # card. Keep the caller's viewport clip, not the stationary card's.
            if state is None:
                painter.setClipPath(card_shape, Qt.ClipOperation.IntersectClip)
            painter.setClipPath(reflected_path, Qt.ClipOperation.IntersectClip)
            painter.setTransform(reflection, True)
            # At rest the back shares the front's color. Keep its physical
            # edge visible without restoring the removed translucent coating.
            painter.setPen(QPen(fold_border, 1.0) if state is None else Qt.PenStyle.NoPen)
            painter.setBrush(front)
            painter.drawPath(card_shape)
            if state is not None:
                # This edge belongs to the lifted sheet, so it must follow
                # the reflection instead of remaining at the original card.
                self._paint_card_outline(painter, QRectF(card), border)
            painter.restore()
            if state is None:
                tangent = QPointF(-normal.y() / length, normal.x() / length)
                painter.save()
                painter.setClipPath(card_shape, Qt.ClipOperation.IntersectClip)
                painter.setPen(QPen(fold_border, 1.0))
                painter.drawLine(midpoint - tangent * 5000, midpoint + tangent * 5000)
                painter.restore()

        painter.restore()
