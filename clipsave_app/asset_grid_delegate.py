from __future__ import annotations

import datetime as dt
import math
from collections import OrderedDict

from PySide6.QtCore import QModelIndex, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import (
    QAbstractTextDocumentLayout, QColor, QFont, QLinearGradient, QPainter,
    QPainterPath, QPalette, QPen, QPixmap, QPolygonF, QTextDocument,
    QTextOption, QTransform,
)
from PySide6.QtWidgets import QStyleOptionViewItem, QStyledItemDelegate

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

    def __init__(self, view):
        super().__init__(view)
        self.view = view
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
        return self.card_rect(rect).adjusted(12, 54, -12, -12)

    def time_rect(self, rect: QRect) -> QRect:
        card = self.card_rect(rect)
        left_inset = max(12, round(card.width() * 0.08))
        return QRect(
            card.left() + left_inset,
            card.top() + 9,
            max(1, card.width() - left_inset - 36),
            24,
        )

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

    def _draw_markdown_preview(self, painter, rect, content, dark, font) -> None:
        document = self._markdown_document(content, rect.width(), dark, font)
        painter.save()
        painter.setClipRect(rect)
        painter.translate(rect.topLeft())
        context = QAbstractTextDocumentLayout.PaintContext()
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
    def _draw_plain_text_preview(painter, rect, content, font) -> None:
        layouts, _lines = AssetGridDelegate._plain_text_layout(
            content, max(1, rect.width()), max(1, rect.height()), font)
        painter.save()
        painter.setClipRect(rect)
        for layout in layouts:
            layout.draw(painter, QPointF(rect.left(), rect.top()))
        painter.restore()

    def transition_layout_signature(self, index, cell_size):
        return self._transition_renderer.layout_signature(index, cell_size)

    def _paint_preview_content(self, painter, preview, index, record, dark, font) -> None:
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
                self._draw_markdown_preview(painter, preview, content, dark, font)
            else:
                self._draw_plain_text_preview(painter, preview, content, font)

    def render_transition_preview(self, index, cell_size, layout_signature=None):
        return self._transition_renderer.render_preview(index, cell_size, layout_signature)

    @staticmethod
    def transition_image_target(preview: QRect, pixmap: QPixmap) -> QRectF:
        return AssetGridTransitionRenderer.image_target(preview, pixmap)

    @staticmethod
    def transition_preview_clip(preview: QRect, kind: str) -> QRect:
        return AssetGridTransitionRenderer.preview_clip(preview, kind)

    @staticmethod
    def _paper_colors(favorite: bool, dark: bool):
        navy = QColor("#292929") if dark else QColor("#ffffff")
        yellow = QColor(247, 195, 63, 210) if dark else QColor(248, 196, 63, 210)
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
    ) -> None:
        card = self.card_rect(option.rect)
        dark = dark_theme_active()
        yellow_paper = isinstance(fill, QColor) and fill.red() > fill.blue() * 2
        paper_rect = QRectF(card)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawPath(self._card_shape_path(paper_rect))
        # Keep the stroke inside the card so its right and bottom edges cannot
        # be clipped by the item's half-open raster bounds.
        if paint_outline:
            self._paint_card_outline(painter, paper_rect, border)
        painter.setFont(option.font)
        painter.setPen(QColor("#34404e" if yellow_paper else "#d9e1eb" if dark else "#45576a"))
        painter.drawText(self.time_rect(option.rect),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         format_card_timestamp(record["last_used_at"]))
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
            painter.setClipRect(self.transition_preview_clip(preview, record["kind"]))
            if record["kind"] == "image":
                painter.drawPixmap(self.transition_image_target(preview, preview_cache),
                                   preview_cache, QRectF(preview_cache.rect()))
            else:
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

    def _sheet_pixmap(self, option, index, record, fill, border, preview_cache) -> QPixmap:
        dpr = max(1.0, float(self.view.devicePixelRatioF()))
        pixmap = QPixmap(max(1, round(option.rect.width() * dpr)),
                         max(1, round(option.rect.height() * dpr)))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)
        copy = QStyleOptionViewItem(option)
        copy.rect = QRect(0, 0, option.rect.width(), option.rect.height())
        local = QPainter(pixmap)
        local.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._paint_sheet(local, copy, index, record, fill, border, preview_cache)
        local.end()
        return pixmap

    def paint(self, painter, option, index) -> None:
        if getattr(self.view, "_sidebar_transition_active", False):
            return
        if getattr(self.view, "paper_peel_state", lambda _row: None)(index.row()) is not None:
            return
        self.paint_transition_card(painter, option, index)

    def paint_transition_card(self, painter, option, index, preview_cache=None) -> None:
        record = index.data(AssetItemModel.ItemRole)
        if record is None:
            return
        dark = dark_theme_active()
        front, back, border = self._paper_colors(bool(record["favorite"]), dark)
        state = getattr(self.view, "paper_peel_state", lambda _row: None)(index.row())
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
        removed_path = _polygon_path(removed)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not single_sheet:
            painter.save()
            painter.setClipPath(card_shape)
            painter.setClipPath(removed_path, Qt.ClipOperation.IntersectClip)
            self._paint_sheet(
                painter, option, index, record, back, border, preview_cache,
                paint_outline=False,
            )
            painter.restore()
        painter.save()
        painter.setClipPath(card_shape)
        painter.setClipPath(retained_path, Qt.ClipOperation.IntersectClip)
        if state is None:
            self._paint_sheet(
                painter, option, index, record, front, border, preview_cache,
                paint_outline=False,
            )
            front_pixmap = None
        else:
            front_pixmap = self._sheet_pixmap(option, index, record, front, border, preview_cache)
            painter.drawPixmap(option.rect.topLeft(), front_pixmap)
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
            # original rounded card path and sheet pixmap then supply the exact
            # source-corner radius without manufacturing a derived path.
            reflected_path = _polygon_path(
                QPolygonF([reflection.map(point) for point in removed])
            )
            if state is None:
                fold_face = QColor(front)
                fold_face.setAlpha(min(190, fold_face.alpha() + 42))
                painter.save()
                painter.setClipPath(card_shape)
                painter.setClipPath(reflected_path, Qt.ClipOperation.IntersectClip)
                painter.setTransform(reflection, True)
                painter.setPen(QPen(QColor(235, 242, 250, 54), 1.0))
                painter.setBrush(fold_face)
                painter.drawPath(card_shape)
                painter.restore()
                tangent = QPointF(-normal.y() / length, normal.x() / length)
                painter.save()
                painter.setClipPath(card_shape)
                painter.setPen(QPen(QColor(235, 242, 250, 54), 1.0))
                painter.drawLine(
                    midpoint - tangent * 5000,
                    midpoint + tangent * 5000,
                )
                painter.restore()
            else:
                painter.save()
                painter.setClipPath(reflected_path)
                painter.setClipPath(card_shape, Qt.ClipOperation.IntersectClip)
                painter.setTransform(reflection, True)
                painter.drawPixmap(option.rect.topLeft(), front_pixmap)
                painter.restore()
                gradient = QLinearGradient(midpoint, cursor)
                gradient.setColorAt(0, QColor(255, 255, 255, 48))
                gradient.setColorAt(0.45, QColor(255, 255, 255, 12))
                gradient.setColorAt(1, QColor(0, 0, 0, 42))
                painter.save()
                painter.setClipPath(reflected_path)
                painter.setTransform(reflection, True)
                source_gradient = QLinearGradient(
                    reflection.map(midpoint),
                    reflection.map(cursor),
                )
                source_gradient.setStops(gradient.stops())
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(source_gradient)
                painter.drawPath(card_shape)
                painter.restore()

        # The two stationary paper layers are painted through complementary
        # clipping paths. Their shared outer boundary excludes the last device
        # pixel on the right and bottom, so draw one unclipped inner outline.
        # A single sheet must not leave that complete outline behind while it
        # is being peeled away; constrain its outline to the retained paper.
        if not single_sheet:
            self._paint_card_outline(painter, QRectF(card), border)
        else:
            # Keep the lifted yellow flap, but do not draw the complete card
            # outline through the exposed corner: that curve would read as a
            # second sheet underneath the favorite card.
            painter.save()
            painter.setClipPath(card_shape)
            painter.setClipPath(retained_path, Qt.ClipOperation.IntersectClip)
            self._paint_card_outline(painter, QRectF(card), border)
            painter.restore()

        if state is not None:
            tangent = QPointF(-normal.y() / length, normal.x() / length)
            painter.save()
            painter.setClipRect(card)
            painter.setPen(QPen(QColor(0, 0, 0, 32), 7.0, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap))
            painter.drawLine(midpoint - tangent * 5000, midpoint + tangent * 5000)
            painter.setPen(QPen(QColor(235, 242, 250, 68), 1.0))
            painter.drawLine(midpoint - tangent * 5000, midpoint + tangent * 5000)
            painter.restore()
        painter.restore()
