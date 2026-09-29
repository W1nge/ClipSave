import datetime as dt
import contextlib
import gc
import io
import math
import unittest
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QBitmap, QColor, QImage, QMouseEvent, QPainter, QPixmap, QRegion
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem, QWidget

from clipsave_app.asset_table import AssetTable
from clipsave_app.asset_grid import AssetGrid, PaperPeelState
from clipsave_app.asset_grid_delegate import AssetGridDelegate, format_card_timestamp
from clipsave_app.asset_grid_transition_controller import AssetGridTransitionController
from clipsave_app.item_models import format_list_timestamp
from clipsave_app.middle_autoscroll import _AutoScrollMarker


class PaperCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def tearDown(self):
        # Release PySide painter/delegate wrappers while the owning widgets
        # from this test are still in a known state.  Deferring them until a
        # later suite-wide collection can double-release Qt native storage on
        # Python 3.11/3.12 and falsely implicate an unrelated subsequent test.
        gc.collect()
        self.app.processEvents()

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
            "last_used_at": "2026-09-22T01:39:00+08:00",
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
            "last_used_at": "2026-09-22T01:39:00+08:00",
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

        class CountingDelegate(AssetGridDelegate):
            def __init__(self, owner):
                super().__init__(owner)
                self.sheet_paint_count = 0

            def _paint_sheet(self, *args, **kwargs):
                self.sheet_paint_count += 1
                return super()._paint_sheet(*args, **kwargs)

        delegate = CountingDelegate(view)
        image = QImage(270, 211, QImage.Format.Format_ARGB32)
        image.fill(QColor("#202020"))
        option = QStyleOptionViewItem()
        option.rect = QRect(2, 2, 266, 207)
        option.font = view.font()
        record = {
            "created_at": "2026-09-22T01:39:00+08:00",
            "last_used_at": "2026-09-22T01:39:00+08:00",
            "kind": "text",
            "favorite": True,
        }
        index = MagicMock()
        index.data.return_value = record
        index.row.return_value = 0
        preview = QPixmap(1, 1)
        preview.fill(Qt.GlobalColor.transparent)

        painter = QPainter(image)
        delegate.paint_transition_card(painter, option, index, preview)
        painter.end()

        self.assertEqual(delegate.sheet_paint_count, 1)
        card = delegate.card_rect(option.rect)
        # No sheet fills the exposed corner. The uncoated flap interior stays
        # the front color, while its physical crease remains distinguishable.
        self.assertEqual(
            image.pixelColor(card.right() - 5, card.top() + 5),
            QColor("#202020"),
        )
        self.assertEqual(
            image.pixelColor(card.right() - 17, card.top() + 16),
            image.pixelColor(card.center().x(), card.top() + 24),
        )
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
        state = PaperPeelState(0, destination, destination)
        view.paper_peel_state = lambda _row: state
        record = {
            "created_at": "2026-09-22T01:39:00+08:00",
            "last_used_at": "2026-09-22T01:39:00+08:00",
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

    def _peel_fixture(self, favorite=False, kind="text"):
        grid = AssetGrid()
        self.addCleanup(grid.close)
        grid.set_items([{
            "id": 1, "kind": kind, "favorite": favorite,
            "content": "纸张下面仍应有完整内容 ABCDEFGHIJKLMNOPQRSTUVWXYZ " * 8,
            "title": "paper", "path": "fixture.png" if kind == "image" else None,
            "created_at": "2026-09-22T08:00:00+08:00",
            "last_used_at": "2026-09-22T08:00:00+08:00",
            "width": 64, "height": 40, "file_size": 128, "tag_names": "",
        }])
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 270, 211)
        option.font = grid.font()
        return grid, option, grid.model().index(0, 0)

    @staticmethod
    def _render_peel(grid, option, index, background, **kwargs):
        dpr = grid.devicePixelRatioF()
        image = QImage(round(option.rect.width() * dpr), round(option.rect.height() * dpr),
                       QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
        image.fill(background)
        painter = QPainter(image)
        grid.delegate.paint_transition_card(painter, option, index, **kwargs)
        painter.end()
        return image

    def test_paper_layers_hide_the_background_throughout_peel(self):
        self.app.setProperty("darkTheme", True)
        for favorite in (False, True):
            grid, option, index = self._peel_fixture(favorite)
            for position in (None, QPointF(80, 190), QPointF(-240, 300)):
                with self.subTest(favorite=favorite, position=position):
                    grid._paper_peel = (PaperPeelState(0, position, position)
                                        if position is not None else None)
                    images = [self._render_peel(grid, option, index, color)
                              for color in (QColor("#ff00ff"), QColor("#00ffff"))]
                    # Every interior pixel of the two-sheet card must hide
                    # the background, including the fold and its seam.
                    dpr = grid.devicePixelRatioF()
                    interior = grid.delegate.card_rect(option.rect).adjusted(10, 10, -10, -10)
                    crop = QRect(round(interior.x() * dpr), round(interior.y() * dpr),
                                 round(interior.width() * dpr), round(interior.height() * dpr))
                    self.assertEqual(images[0].copy(crop), images[1].copy(crop))

    def test_exposed_bottom_sheet_keeps_content_and_its_own_text_color(self):
        self.app.setProperty("darkTheme", True)
        for kind in ("text", "markdown", "image"):
            for favorite in (False, True):
                with self.subTest(kind=kind, favorite=favorite):
                    grid, option, index = self._peel_fixture(favorite, kind)
                    grid._paper_peel = PaperPeelState(0, QPointF(80, 190), QPointF(80, 190))
                    source = QPixmap(64, 40)
                    source.fill(QColor("#ef2a78"))
                    grid.isVisible = lambda: True
                    grid.thumbnail_for_index = lambda *_args: source
                    actual = self._render_peel(grid, option, index, Qt.GlobalColor.transparent)
                    # Compare an exposed area far from the moving fold with
                    # the complete underlying sheet, including timestamp.
                    expected = QImage(actual.size(), actual.format())
                    expected.setDevicePixelRatio(actual.devicePixelRatio())
                    expected.fill(Qt.GlobalColor.transparent)
                    state = grid._paper_peel
                    grid._paper_peel = None
                    grid.model().set_favorite(1, not favorite)
                    painter = QPainter(expected)
                    grid.delegate.paint(painter, option, index)
                    painter.end()
                    grid.model().set_favorite(1, favorite)
                    grid._paper_peel = state
                    dpr = actual.devicePixelRatio()
                    for rect in (QRect(210, 65, 30, 55), QRect(24, 15, 70, 24)):
                        if rect.top() < 50:
                            # Peel further so that the timestamp is exposed.
                            grid._paper_peel.current = QPointF(-400, 420)
                            actual = self._render_peel(grid, option, index, Qt.GlobalColor.transparent)
                        crop = QRect(round(rect.x() * dpr), round(rect.y() * dpr),
                                     round(rect.width() * dpr), round(rect.height() * dpr))
                        self.assertEqual(actual.copy(crop), expected.copy(crop))

    def test_fold_back_is_unprinted_and_opaque(self):
        self.app.setProperty("darkTheme", True)
        grid, option, index = self._peel_fixture()
        grid._paper_peel = PaperPeelState(0, QPointF(80, 190), QPointF(80, 190))
        # The same face rendered with/without printed content must have an
        # identical back. This catches reflected text and mirrored images.
        printed = self._render_peel(grid, option, index, Qt.GlobalColor.transparent)
        record = dict(index.data(grid.model().ItemRole))
        record.update(content="", title="")
        grid.set_items([record])
        grid._paper_peel = PaperPeelState(0, QPointF(80, 190), QPointF(80, 190))
        blank = self._render_peel(grid, option, grid.model().index(0, 0), Qt.GlobalColor.transparent)
        dpr = printed.devicePixelRatio()
        region = QRect(round(150 * dpr), round(125 * dpr), round(35 * dpr), round(35 * dpr))
        self.assertEqual(printed.copy(region), blank.copy(region))

    def test_lifted_paper_extends_outside_card_but_respects_viewport_clip(self):
        for dark in (False, True):
            self.app.setProperty('darkTheme', dark)
            for favorite in (False, True):
                grid, option, index = self._peel_fixture(favorite)
                option.rect.moveTo(140, 30)
                position = QPointF(-70, 270)
                grid._paper_peel = PaperPeelState(0, position, position)
                for clipped in (False, True):
                    with self.subTest(dark=dark, favorite=favorite, clipped=clipped):
                        image = QImage(480, 430, QImage.Format.Format_ARGB32_Premultiplied)
                        image.fill(Qt.GlobalColor.transparent)
                        painter = QPainter(image)
                        if clipped:
                            painter.setClipRect(QRect(100, 0, 380, 430))
                        grid.delegate.paint_transition_card(painter, option, index)
                        painter.end()
                        self.assertTrue(any(image.pixelColor(x, y).alpha() == 255
                                            for x in range(101, 135) for y in range(200, 340)),
                                        'The lifted back was cut off at the original card boundary')
                        if clipped:
                            self.assertTrue(all(image.pixelColor(x, y).alpha() == 0
                                                for x in range(0, 100) for y in range(430)))

    def test_card_outline_cannot_show_through_the_lifted_back(self):
        self.app.setProperty('darkTheme', True)
        for favorite in (False, True):
            grid, option, index = self._peel_fixture(favorite)
            position = QPointF(-70, 270)
            grid._paper_peel = PaperPeelState(0, position, position)
            image = self._render_peel(grid, option, index, Qt.GlobalColor.transparent)
            card = grid.delegate.card_rect(option.rect)
            front, _, _ = grid.delegate._paper_colors(favorite, True)
            dpr = image.devicePixelRatio()
            self.assertEqual(image.pixelColor(round(card.left() * dpr),
                                              round((card.top() + 80) * dpr)), front)

    def test_lifted_sheet_keeps_its_own_outline_outside_card(self):
        for dark in (False, True):
            self.app.setProperty('darkTheme', dark)
            for favorite in (False, True):
                grid, option, index = self._peel_fixture(favorite)
                option.rect.moveTo(140, 30)
                position = QPointF(-70, 270)
                grid._paper_peel = PaperPeelState(0, position, position)
                front, _, _ = grid.delegate._paper_colors(favorite, dark)
                dpr = grid.devicePixelRatioF()
                for single_sheet in (False, True):
                    grid.set_favorite_page_mode(single_sheet)
                    grid._paper_peel = PaperPeelState(0, position, position)
                    with self.subTest(dark=dark, favorite=favorite, single_sheet=single_sheet):
                        image = QImage(round(480 * dpr), round(430 * dpr),
                                       QImage.Format.Format_ARGB32_Premultiplied)
                        image.setDevicePixelRatio(dpr)
                        # On matching paper/background colors, only the
                        # physical edge distinguishes the lifted sheet.
                        image.fill(front)
                        painter = QPainter(image)
                        grid.delegate.paint_transition_card(painter, option, index, paint_content=False)
                        painter.end()
                        card = grid.delegate.card_rect(option.rect)
                        for side, area in (
                            ('left', QRect(65, 50, card.left() - 68, 300)),
                            ('bottom', QRect(65, card.bottom() + 4, 300, 110)),
                        ):
                            visible = 0
                            for y in range(round(area.top() * dpr), round((area.y() + area.height()) * dpr)):
                                for x in range(round(area.left() * dpr), round((area.x() + area.width()) * dpr)):
                                    color = image.pixelColor(x, y)
                                    if max(abs(color.red() - front.red()), abs(color.green() - front.green()),
                                           abs(color.blue() - front.blue())) >= 5:
                                        visible += 1
                            self.assertGreater(visible, 30, f'The lifted {side} edge disappeared')

    def test_departure_finishes_before_single_commit_and_new_corner_is_opaque(self):
        grid, option, index = self._peel_fixture()
        card = grid.delegate.card_rect(option.rect)
        corner = grid.delegate.paper_corner(card)
        start = corner + QPointF(-grid.delegate.CORNER_SIZE, grid.delegate.CORNER_SIZE)
        end = corner + (QPointF(card.left(), card.bottom()) - corner) * 2.04
        grid._paper_peel = PaperPeelState(0, start, start)
        requests = []
        grid.favorite_requested.connect(lambda *args: requests.append(args))
        with patch('clipsave_app.asset_grid.time.monotonic', return_value=10.0):
            grid._animate_paper_peel(end, 288, True, curved=True)
        grid._paper_animation.stop()
        with patch('clipsave_app.asset_grid.time.monotonic', return_value=10.2):
            grid._advance_paper_animation()
        self.assertEqual(grid._paper_peel.departure_progress, 0.0)
        with patch('clipsave_app.asset_grid.time.monotonic', return_value=10.468):
            grid._advance_paper_animation()
        self.assertEqual(grid._paper_peel.current, end)
        self.assertAlmostEqual(grid._paper_peel.departure_progress, 0.5)
        self.assertEqual(requests, [])
        with patch('clipsave_app.asset_grid.time.monotonic', return_value=10.7):
            grid._advance_paper_animation()
            grid._advance_paper_animation()
        self.assertEqual(grid._paper_peel.departure_progress, 1.0)
        self.assertEqual(requests, [(1, True)])
        grid.preview_favorite_change(1, True)
        self.assertEqual(grid._paper_peel.departure_progress, 0.0)
        self.assertFalse(grid._paper_peel.commit)

    def test_drag_release_keeps_all_eight_directions_until_fully_detached(self):
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1),
                       (-1, -1), (-1, 1), (1, -1), (1, 1)):
            with self.subTest(direction=(dx, dy)):
                grid, option, index = self._peel_fixture()
                grid.visualRect = lambda _index: QRect(option.rect)
                card = grid.delegate.card_rect(option.rect)
                corner = grid.delegate.paper_corner(card)
                diagonal = math.hypot(card.width(), card.height())
                press = corner + QPointF(-12, 12)
                grid._start_paper_peel(index, press.toPoint())
                release = (corner + QPointF(dx, dy) * (diagonal * 0.7)).toPoint()
                # No move is delivered: the release must use its own position.
                event = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(release),
                                    QPointF(release), Qt.MouseButton.LeftButton,
                                    Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
                with patch('clipsave_app.asset_grid.time.monotonic', return_value=10.0):
                    grid.mouseReleaseEvent(event)
                grid._paper_animation.stop()
                state = grid._paper_peel
                self.assertTrue(state.commit)
                self.assertFalse(state.animation_curved)
                self.assertEqual(state.animation_start, QPointF(release))
                start_ray = state.current - corner
                end_ray = state.animation_end - corner
                self.assertAlmostEqual(start_ray.x() * end_ray.y() - start_ray.y() * end_ray.x(), 0.0)
                self.assertGreater(QPointF.dotProduct(state.animation_end - state.current, start_ray), 0.0)
                # No original card point can remain on the retained side of
                # the terminal fold, even when dragging toward the outside.
                midpoint = (corner + state.animation_end) / 2.0
                bounds = QRectF(card)
                for point in (bounds.topLeft(), bounds.topRight(), bounds.bottomLeft(), bounds.bottomRight()):
                    self.assertLess(QPointF.dotProduct(point - midpoint, end_ray), 0.0)
                requests = []
                grid.favorite_requested.connect(lambda *args: requests.append(args))
                with patch('clipsave_app.asset_grid.time.monotonic', return_value=10.217):
                    grid._advance_paper_animation()
                self.assertEqual(state.current, state.animation_end)
                self.assertLess(state.departure_progress, 0.001)
                with patch('clipsave_app.asset_grid.time.monotonic', return_value=10.6):
                    grid._advance_paper_animation()
                    grid._advance_paper_animation()
                self.assertEqual(state.departure_progress, 1.0)
                self.assertEqual(requests, [(1, True)])

    def test_detached_fading_sheet_moves_in_each_cardinal_drag_direction(self):
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            with self.subTest(direction=(dx, dy)):
                grid, option, index = self._peel_fixture(True)
                grid.set_favorite_page_mode(True)
                card = grid.delegate.card_rect(option.rect)
                corner = grid.delegate.paper_corner(card)
                diagonal = math.hypot(card.width(), card.height())
                end = corner + QPointF(dx, dy) * (2.05 * diagonal)
                grid._paper_peel = PaperPeelState(0, end, end)
                option.rect.moveTo(900, 900)
                silhouettes = []
                for progress in (0.0, 0.4, 1.0):
                    grid._paper_peel.departure_progress = progress
                    canvas = QImage(2200, 2200, QImage.Format.Format_ARGB32_Premultiplied)
                    canvas.fill(Qt.GlobalColor.transparent)
                    painter = QPainter(canvas)
                    grid.delegate.paint_transition_card(painter, option, index, paint_content=False)
                    painter.end()
                    silhouettes.append(QRegion(QBitmap.fromImage(canvas.createAlphaMask())).boundingRect())
                self.assertFalse(silhouettes[0].isEmpty())
                self.assertFalse(silhouettes[1].isEmpty())
                movement = silhouettes[1].center() - silhouettes[0].center()
                self.assertGreater(movement.x() * dx + movement.y() * dy, 5)
                self.assertLessEqual(abs(movement.x() * dy - movement.y() * dx), 1)
                self.assertTrue(silhouettes[2].isEmpty())

    def test_detached_sheet_fades_without_fading_the_revealed_card(self):
        self.app.setProperty('darkTheme', True)
        grid, option, index = self._peel_fixture()
        local = grid.delegate.card_rect(option.rect)
        corner = grid.delegate.paper_corner(local)
        end = corner + (QPointF(local.left(), local.bottom()) - corner) * 2.04
        option.rect.moveTo(350, 40)
        grid._paper_peel = PaperPeelState(0, end, end)
        images, alpha = [], []
        for progress in (0.0, 0.5, 1.0):
            grid._paper_peel.departure_progress = progress
            canvas = QImage(720, 480, QImage.Format.Format_ARGB32_Premultiplied)
            canvas.fill(Qt.GlobalColor.transparent)
            painter = QPainter(canvas)
            grid.delegate.paint_transition_card(painter, option, index)
            painter.end()
            images.append(canvas)
            below = canvas.copy(QRect(0, 260, 720, 220)).convertToFormat(QImage.Format.Format_Alpha8)
            alpha.append(sum(bytes(below.constBits())))
        self.assertGreater(alpha[0], alpha[1])
        self.assertGreater(alpha[1], 0)
        self.assertEqual(alpha[2], 0)
        card = grid.delegate.card_rect(option.rect).adjusted(10, 10, -10, -10)
        self.assertEqual(images[0].copy(card), images[1].copy(card))
        self.assertEqual(images[0].copy(card), images[2].copy(card))


if __name__ == "__main__":
    unittest.main()
