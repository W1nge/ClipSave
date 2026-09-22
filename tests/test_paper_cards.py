import datetime as dt
import contextlib
import io
import unittest
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem, QWidget

from clipsave_app.asset_table import AssetTable
from clipsave_app.asset_grid_delegate import AssetGridDelegate, format_card_timestamp
from clipsave_app.asset_grid_transition_controller import AssetGridTransitionController
from clipsave_app.item_models import format_list_timestamp
from clipsave_app.middle_autoscroll import _AutoScrollMarker


class PaperCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_timestamp_uses_time_then_short_and_cross_year_dates(self):
        timezone = dt.timezone(dt.timedelta(hours=8))
        now = dt.datetime(2026, 9, 21, 18, 0, tzinfo=timezone)

        self.assertEqual(format_card_timestamp("2026-09-21T10:41:00+08:00", now), "10:41")
        self.assertEqual(format_card_timestamp("2026-09-21T02:41:00+00:00", now), "10:41")
        self.assertEqual(format_card_timestamp("2026-09-20T10:41:00+08:00", now), "09/20")
        self.assertEqual(format_card_timestamp("2025-12-31T23:59:00+08:00", now), "2025/12/31")

    def test_list_timestamp_stays_compact_but_keeps_useful_precision(self):
        timezone = dt.timezone(dt.timedelta(hours=8))
        now = dt.datetime(2026, 9, 21, 18, 0, tzinfo=timezone)

        self.assertEqual(format_list_timestamp("2026-09-21T10:41:00+08:00", now), "10:41")
        self.assertEqual(format_list_timestamp("2026-09-21T02:41:00+00:00", now), "10:41")
        self.assertEqual(format_list_timestamp("2026-09-20T10:41:00+08:00", now), "09/20 10:41")
        self.assertEqual(format_list_timestamp("2025-12-31T23:59:00+08:00", now), "2025/12/31")

    def test_typical_expanded_home_width_uses_four_proportional_cards(self):
        columns, cell = AssetGridTransitionController.layout_for_viewport_width(1100)

        self.assertEqual(columns, 4)
        card_width = cell.width() - 12
        card_height = cell.height() - 12
        self.assertAlmostEqual(card_width / card_height, 183 / 141, delta=0.02)

    def test_library_view_keeps_rounded_scrollbar_and_middle_auto_scroll(self):
        table = AssetTable()
        table.resize(480, 320)
        table.show()
        self.app.processEvents()

        self.assertEqual(table.verticalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        point = QPoint(120, 100)
        QTest.mouseClick(table.viewport(), Qt.MouseButton.MiddleButton, pos=point)
        self.assertTrue(table._middle_autoscroll.active)
        QTest.mouseClick(table.viewport(), Qt.MouseButton.MiddleButton, pos=point)
        self.assertFalse(table._middle_autoscroll.active)
        table.close()

    def test_middle_auto_scroll_accumulates_subpixel_low_speed_motion(self):
        table = AssetTable()
        table.resize(480, 320)
        table.show()
        self.app.processEvents()
        bar = table.verticalScrollBar()
        bar.setRange(0, 100)
        controller = table._middle_autoscroll
        anchor = QPoint(40, 40)
        controller._start(anchor)
        controller._timer.stop()
        cursor = table.viewport().mapToGlobal(anchor + QPoint(0, controller.DEAD_ZONE + 1))

        with patch("clipsave_app.middle_autoscroll.QCursor.pos", return_value=cursor):
            for _ in range(20):
                controller._tick()

        self.assertGreater(bar.value(), 0)
        controller.cancel()
        table.close()

    def test_middle_auto_scroll_marker_paints_without_qpainter_errors(self):
        parent = QWidget()
        parent.resize(80, 80)
        marker = _AutoScrollMarker(parent)
        marker.move(20, 20)
        parent.show()
        marker.show()
        errors = io.StringIO()

        with contextlib.redirect_stderr(errors):
            marker.repaint()
            self.app.processEvents()

        output = errors.getvalue()
        self.assertNotIn("drawPolygon", output)
        self.assertNotIn("Painter not active", output)
        self.assertNotIn("active painter", output)
        parent.close()

    def test_static_paper_keeps_all_four_outer_edges(self):
        self.app.setProperty("darkTheme", True)
        view = QWidget()
        view.paper_peel_state = lambda _row: None
        delegate = AssetGridDelegate(view)
        image = QImage(270, 211, QImage.Format.Format_ARGB32)
        background = QColor(9, 10, 10)
        image.fill(background)
        option = QStyleOptionViewItem()
        option.rect = QRect(2, 2, 266, 207)
        option.font = view.font()
        record = {
            "created_at": "2026-09-22T01:39:00+08:00",
            "kind": "text",
            "favorite": False,
        }
        index = MagicMock()
        index.data.return_value = record
        index.row.return_value = 0
        preview = QPixmap(1, 1)
        preview.fill(Qt.GlobalColor.transparent)

        painter = QPainter(image)
        delegate.paint_transition_card(painter, option, index, preview)
        painter.end()

        card = delegate.card_rect(option.rect)
        edge_colors = [
            image.pixelColor(card.left(), card.center().y()).rgba(),
            image.pixelColor(card.right(), card.center().y()).rgba(),
            image.pixelColor(card.center().x(), card.top()).rgba(),
            image.pixelColor(card.center().x(), card.bottom()).rgba(),
        ]
        self.assertEqual(len(set(edge_colors)), 1)
        self.assertNotEqual(edge_colors[0], background.rgba())
        view.close()

    def test_first_layout_row_keeps_timestamp_and_divider(self):
        self.app.setProperty("darkTheme", True)
        view = QWidget()
        view.paper_peel_state = lambda _row: None
        delegate = AssetGridDelegate(view)
        image = QImage(270, 211, QImage.Format.Format_ARGB32)
        image.fill(QColor("#181818"))
        option = QStyleOptionViewItem()
        option.rect = QRect(2, 0, 266, 207)
        option.font = view.font()
        record = {
            "created_at": "2026-09-22T01:39:00+08:00",
            "kind": "text",
            "favorite": False,
        }
        index = MagicMock()
        index.data.return_value = record
        index.row.return_value = 0
        preview = QPixmap(1, 1)
        preview.fill(Qt.GlobalColor.transparent)

        painter = QPainter(image)
        delegate.paint_transition_card(painter, option, index, preview)
        painter.end()

        time_rect = delegate.time_rect(option.rect)
        bright_header_pixels = sum(
            image.pixelColor(x, y).lightness() > 80
            for y in range(time_rect.top(), time_rect.bottom() + 1)
            for x in range(time_rect.left(), time_rect.right() + 1)
        )
        self.assertGreater(bright_header_pixels, 10)
        card = delegate.card_rect(option.rect)
        self.assertNotEqual(
            image.pixelColor(card.center().x(), card.top() + 41).name(),
            "#181818",
        )
        view.close()

    def test_favorite_page_card_does_not_paint_a_backing_sheet(self):
        self.app.setProperty("darkTheme", True)
        view = QWidget()
        view.favorite_page_mode = True
        view.paper_peel_state = lambda _row: None
        delegate = AssetGridDelegate(view)
        image = QImage(270, 211, QImage.Format.Format_ARGB32)
        image.fill(QColor("#202020"))
        option = QStyleOptionViewItem()
        option.rect = QRect(2, 2, 266, 207)
        option.font = view.font()
        record = {
            "created_at": "2026-09-22T01:39:00+08:00",
            "kind": "text",
            "favorite": True,
        }
        index = MagicMock()
        index.data.return_value = record
        index.row.return_value = 0
        preview = QPixmap(1, 1)
        preview.fill(Qt.GlobalColor.transparent)

        with patch.object(delegate, "_paint_sheet", wraps=delegate._paint_sheet) as paint_sheet:
            painter = QPainter(image)
            delegate.paint_transition_card(painter, option, index, preview)
            painter.end()

        self.assertEqual(paint_sheet.call_count, 1)
        card = delegate.card_rect(option.rect)
        # The yellow flap remains visible, but only one sheet body is painted.
        self.assertNotEqual(
            image.pixelColor(card.right() - 12, card.top() + 12),
            image.pixelColor(card.center().x(), card.top() + 24),
        )
        view.close()

    def test_favorite_page_card_leaves_no_body_after_completed_peel(self):
        self.app.setProperty("darkTheme", True)
        view = QWidget()
        view.favorite_page_mode = True
        delegate = AssetGridDelegate(view)
        option = QStyleOptionViewItem()
        option.rect = QRect(2, 2, 266, 207)
        option.font = view.font()
        local_card = delegate.card_rect(QRect(0, 0, 266, 207))
        corner = delegate.paper_corner(local_card)
        destination = corner + (
            QPointF(local_card.left(), local_card.bottom()) - corner
        ) * 2.15
        state = MagicMock()
        state.current = destination
        view.paper_peel_state = lambda _row: state
        record = {
            "created_at": "2026-09-22T01:39:00+08:00",
            "kind": "text",
            "favorite": True,
        }
        index = MagicMock()
        index.data.return_value = record
        index.row.return_value = 0
        preview = QPixmap(1, 1)
        preview.fill(Qt.GlobalColor.transparent)
        background = QColor("#202020")
        image = QImage(270, 211, QImage.Format.Format_ARGB32)
        image.fill(background)

        painter = QPainter(image)
        delegate.paint_transition_card(painter, option, index, preview)
        painter.end()

        card = delegate.card_rect(option.rect)
        for point in (
            card.center(),
            QPoint(card.left() + 10, card.top() + 10),
            QPoint(card.right() - 10, card.top() + 10),
            QPoint(card.left() + 10, card.bottom() - 10),
            QPoint(card.right() - 10, card.bottom() - 10),
        ):
            self.assertEqual(image.pixelColor(point), background)
        view.close()


if __name__ == "__main__":
    unittest.main()
