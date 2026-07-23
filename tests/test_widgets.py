import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QByteArray, QEvent, QPoint, QPointF, QPropertyAnimation, QRect, QSize, QThread, Qt, QUrl
from PySide6.QtGui import QColor, QEnterEvent, QImage, QPainter, QPixmap, QTextDocument, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QLabel,
    QProxyStyle,
    QScrollArea,
    QStyle,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

import clipsave_app.widgets as widgets_module
from clipsave_app.app import create_app_icon
from clipsave_app.styles import DARK_STYLESHEET
from clipsave_app.widgets import (
    AssetGrid,
    AssetTable,
    AutoHideScrollBar,
    CopyToast,
    DateDialog,
    DetailPanel,
    FluentComboBox,
    FluentMessageDialog,
    MarkdownDialog,
    MAX_RICH_MARKDOWN_BYTES,
    Sidebar,
    SettingsDialog,
    TextDialog,
    _SafeMarkdownBrowser,
    _THUMBNAIL_CACHE,
    _WheelRemainder,
    _half_speed_wheel_event,
    _startfile_or_warn,
    thumbnail_pixmap,
)


def asset_records(count: int, *, kind: str = "text", path: str | None = None) -> list[dict]:
    return [
        {
            "id": index + 1,
            "title": f"asset {index + 1}",
            "kind": kind,
            "path": path,
            "content": "preview text",
            "favorite": index % 2,
            "created_at": "2026-07-12T10:00:00",
            "width": 64 if kind == "image" else 0,
            "height": 40 if kind == "image" else 0,
            "file_size": 128,
            "tag_names": "",
        }
        for index in range(count)
    ]


def wait_for(predicate, timeout: float = 2.0) -> bool:
    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        QTest.qWait(10)
    app.processEvents()
    return bool(predicate())


class ThumbnailPixmapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_copy_toast_is_bottom_right_nonblocking_and_animates_out(self):
        parent = QWidget()
        parent.resize(900, 600)
        parent.show()
        toast = CopyToast(parent)
        toast.reposition()

        self.assertEqual(toast.pos(), QPoint(612, 522))
        self.assertFalse(toast.isVisible())
        self.assertTrue(
            toast.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        )
        self.assertEqual(toast.message_label.text(), "已复制到剪贴板")
        self.assertEqual(toast._hide_timer.interval(), 2300)

        toast.show_confirmation()
        self.app.processEvents()
        self.assertTrue(toast.isVisible())
        self.assertTrue(toast._hide_timer.isActive())

        toast._hide_timer.stop()
        toast._motion.stop()
        toast._fade.stop()
        toast._opacity_effect.setOpacity(1.0)
        toast._begin_hide()
        self.assertTrue(wait_for(lambda: not toast.isVisible(), timeout=0.5))
        parent.close()

    def setUp(self):
        _THUMBNAIL_CACHE.clear()
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        _THUMBNAIL_CACHE.clear()
        self.temp.cleanup()

    def test_thumbnail_decode_runs_off_ui_thread_and_pixmap_cache_runs_on_ui_thread(self):
        path = Path(self.temp.name) / "large.png"
        image = QImage(1440, 900, QImage.Format.Format_RGB32)
        image.fill(QColor("#21a8fb"))
        self.assertTrue(image.save(str(path)))
        grid = AssetGrid()
        grid.resize(520, 400)
        grid.show()
        decode_threads = []
        cache_threads = []
        decode_thumbnail = widgets_module._decode_thumbnail_image
        cache_thumbnail = widgets_module._cache_decoded_thumbnail

        def tracked_decode(key):
            decode_threads.append(QThread.currentThread() is self.app.thread())
            return decode_thumbnail(key)

        def tracked_cache(key, decoded_image):
            cache_threads.append(QThread.currentThread() is self.app.thread())
            return cache_thumbnail(key, decoded_image)

        with (
            patch("clipsave_app.widgets._decode_thumbnail_image", side_effect=tracked_decode),
            patch("clipsave_app.widgets._cache_decoded_thumbnail", side_effect=tracked_cache),
        ):
            grid.set_items(asset_records(1, kind="image", path=str(path)))
            self.assertTrue(wait_for(lambda: not thumbnail_pixmap(path).isNull()))

        first = thumbnail_pixmap(path)
        second = thumbnail_pixmap(path)

        self.assertEqual(decode_threads, [False])
        self.assertEqual(cache_threads, [True])
        self.assertFalse(first.isNull())
        self.assertLessEqual(first.width(), 360)
        self.assertLessEqual(first.height(), 180)
        self.assertEqual(first.cacheKey(), second.cacheKey())
        self.assertEqual(len(_THUMBNAIL_CACHE), 1)
        grid.close()

    def test_home_view_wheel_delta_is_halved(self):
        event = QWheelEvent(
            QPointF(10, 10),
            QPointF(10, 10),
            QPoint(0, 18),
            QPoint(0, 120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )

        scaled = _half_speed_wheel_event(event)

        self.assertEqual(scaled.pixelDelta(), QPoint(0, 9))
        self.assertEqual(scaled.angleDelta(), QPoint(0, 60))

    def test_home_views_do_not_show_title_tooltips(self):
        for view in (AssetGrid(), AssetTable()):
            with self.subTest(view=type(view).__name__):
                view.set_items(asset_records(1))
                index = view.model().index(0, 0)
                self.assertIsNone(index.data(Qt.ItemDataRole.ToolTipRole))
                view.close()

    def test_markdown_card_preview_renders_format_instead_of_source_markers(self):
        grid = AssetGrid()
        try:
            document = grid.delegate._markdown_document(
                "# ClipSave\n\n---\n\n**00:01:43**",
                220,
                True,
                grid.font(),
            )

            plain = document.toPlainText()
            self.assertIn("ClipSave", plain)
            self.assertIn("00:01:43", plain)
            self.assertNotIn("#", plain)
            self.assertNotIn("**", plain)
        finally:
            grid.close()

    def test_dark_markdown_card_draws_light_text(self):
        grid = AssetGrid()
        canvas = QImage(240, 180, QImage.Format.Format_RGB32)
        canvas.fill(QColor("#262626"))
        painter = QPainter(canvas)
        try:
            grid.delegate._draw_markdown_preview(
                painter,
                QRect(8, 8, 220, 160),
                "# ClipSave\n\n**00:19:22**\n\nC:\\\\Users\\\\Winge",
                True,
                grid.font(),
            )
        finally:
            painter.end()
            grid.close()

        light_pixels = sum(
            1
            for x in range(canvas.width())
            for y in range(canvas.height())
            if canvas.pixelColor(x, y).lightness() >= 140
        )
        self.assertGreater(light_pixels, 40)

    def test_grid_preview_uses_equal_left_right_and_bottom_insets(self):
        grid = AssetGrid()
        try:
            item_rect = QRect(0, 0, 260, grid.delegate.card_height + 12)
            card = grid.delegate.card_rect(item_rect)
            preview = grid.delegate.preview_rect(item_rect)

            self.assertEqual(preview.left() - card.left(), 10)
            self.assertEqual(card.right() - preview.right(), 10)
            self.assertEqual(card.bottom() - preview.bottom(), 10)
            self.assertEqual(preview.top() - card.top(), 42)
        finally:
            grid.close()

    def test_touchpad_fractional_wheel_delta_accumulates_between_events(self):
        remainder = _WheelRemainder()
        event = QWheelEvent(
            QPointF(10, 10),
            QPointF(10, 10),
            QPoint(0, 1),
            QPoint(0, 0),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.ScrollUpdate,
            False,
        )

        first = _half_speed_wheel_event(event, remainder)
        second = _half_speed_wheel_event(event, remainder)

        self.assertEqual(first.pixelDelta(), QPoint(0, 0))
        self.assertEqual(second.pixelDelta(), QPoint(0, 1))

    def test_auto_hide_scrollbar_stays_visible_while_hovered(self):
        scroll_bar = AutoHideScrollBar()
        scroll_bar.setRange(0, 100)

        scroll_bar.enterEvent(
            QEnterEvent(QPointF(2, 2), QPointF(2, 2), QPointF(2, 2))
        )
        self.assertTrue(scroll_bar.active)
        self.assertFalse(scroll_bar.hide_timer.isActive())

        scroll_bar.leaveEvent(QEvent(QEvent.Type.Leave))
        self.assertTrue(scroll_bar.active)
        self.assertTrue(scroll_bar.hide_timer.isActive())
        scroll_bar.close()

    def test_auto_hide_scrollbar_active_state_is_fully_custom_painted(self):
        previous_dark_theme = self.app.property("darkTheme")
        scroll_bar = AutoHideScrollBar()
        try:
            self.app.setProperty("darkTheme", True)
            scroll_bar.resize(14, 140)
            scroll_bar.setRange(0, 100)
            scroll_bar.setPageStep(25)
            scroll_bar.setValue(40)
            scroll_bar.set_active(True)

            image = QImage(scroll_bar.size(), QImage.Format.Format_ARGB32)
            image.fill(Qt.GlobalColor.transparent)
            scroll_bar.render(image)

            colors = {
                image.pixelColor(x, y).name()
                for x in range(image.width())
                for y in range(image.height())
            }
            self.assertEqual(colors, {"#202020", "#777777"})
            handle = scroll_bar._handle_rect()
            self.assertEqual(handle.width(), 10)
            self.assertGreaterEqual(handle.height(), 32)
        finally:
            scroll_bar.close()
            self.app.setProperty("darkTheme", previous_dark_theme)

    def test_compact_auto_hide_scrollbar_keeps_the_same_proportions(self):
        scroll_bar = AutoHideScrollBar(track_width=8, light_background="#ffffff")
        try:
            scroll_bar.resize(8, 140)
            scroll_bar.setRange(0, 100)
            scroll_bar.setPageStep(25)
            scroll_bar.set_active(True)

            self.assertEqual(scroll_bar.width(), 8)
            self.assertEqual(scroll_bar._handle_rect().width(), 6)
        finally:
            scroll_bar.close()

    def test_dark_combo_box_has_no_native_drop_down_edge(self):
        previous_stylesheet = self.app.styleSheet()
        previous_dark_theme = self.app.property("darkTheme")
        combo = FluentComboBox()
        try:
            self.app.setProperty("darkTheme", True)
            self.app.setStyleSheet(DARK_STYLESHEET)
            combo.addItem("未分类")
            combo.resize(220, 36)
            combo.show()
            self.app.processEvents()
            image = QImage(combo.size(), QImage.Format.Format_ARGB32)
            image.fill(Qt.GlobalColor.transparent)
            combo.render(image)
            right_strip = [
                image.pixelColor(x, y).name()
                for x in range(image.width() - 6, image.width() - 1)
                for y in range(image.height())
            ]
            self.assertNotIn("#0e0e0e", right_strip)
            self.assertGreater(
                sum(color not in {"#292929", "#505050", "#202020"} for color in right_strip),
                4,
            )
        finally:
            combo.close()
            self.app.setStyleSheet(previous_stylesheet)
            self.app.setProperty("darkTheme", previous_dark_theme)

    def test_fluent_message_dialog_uses_dark_surface_and_safe_delete_default(self):
        previous_stylesheet = self.app.styleSheet()
        previous_dark_theme = self.app.property("darkTheme")
        dialog = FluentMessageDialog(
            "删除标签",
            "确定删除标签吗？\n\n剪贴板内容不会被删除。",
            kind="question",
            accept_text="删除",
            cancel_text="取消",
            destructive=True,
            default_accept=False,
        )
        try:
            self.app.setProperty("darkTheme", True)
            self.app.setStyleSheet(DARK_STYLESHEET)
            dialog.show()
            self.app.processEvents()

            self.assertTrue(dialog.windowFlags() & Qt.WindowType.FramelessWindowHint)
            self.assertEqual(dialog.objectName(), "FluentDialog")
            self.assertTrue(dialog.property("messageDialog"))
            self.assertEqual(dialog.accept_button.objectName(), "Danger")
            self.assertTrue(dialog.cancel_button.isDefault())
            self.assertFalse(dialog.accept_button.isDefault())
            image = QImage(dialog.size(), QImage.Format.Format_ARGB32)
            image.fill(Qt.GlobalColor.transparent)
            dialog.render(image)
            self.assertEqual(image.pixelColor(240, 100).name(), "#202020")
        finally:
            dialog.close()
            self.app.setStyleSheet(previous_stylesheet)
            self.app.setProperty("darkTheme", previous_dark_theme)

    def test_grid_delegate_accepts_sqlite_rows_from_production_queries(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """SELECT 1 AS id, 'image' AS kind, 'row image' AS title,
                      'missing.png' AS path, '' AS content, 0 AS favorite,
                      '2026-07-12T10:00:00' AS created_at, 64 AS width,
                      40 AS height, 128 AS file_size, '' AS tag_names,
                      'hash' AS content_hash"""
        ).fetchone()
        grid = AssetGrid()
        grid.resize(520, 400)
        grid.show()
        grid.set_items([row])
        canvas = QImage(300, 280, QImage.Format.Format_ARGB32)
        canvas.fill(Qt.GlobalColor.transparent)
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 280, 270)
        painter = QPainter(canvas)
        with patch.object(grid, "thumbnail_for_index", return_value=None):
            grid.delegate.paint(painter, option, grid.model().index(0, 0))
        painter.end()
        grid.close()
        connection.close()

    def test_image_right_click_gesture_accepts_sqlite_rows(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """SELECT 1 AS id, 'image' AS kind, 'row image' AS title,
                      'missing.png' AS path, '' AS content, 0 AS favorite,
                      '2026-07-12T10:00:00' AS created_at, 64 AS width,
                      40 AS height, 128 AS file_size, '' AS tag_names"""
        ).fetchone()
        previous_interval = QApplication.doubleClickInterval()
        QApplication.setDoubleClickInterval(20)
        try:
            for view in (AssetGrid(), AssetTable()):
                view.resize(720, 400)
                view.show()
                view.set_items([row])
                self.app.processEvents()
                index = view.model().index(0, 0)
                point = (
                    view.delegate.card_rect(view.visualRect(index)).center()
                    if isinstance(view, AssetGrid)
                    else view.visualRect(index).center()
                )
                detail = Mock()
                view.detail_requested.connect(detail)
                self.assertEqual(view._image_id_at(point), 1)
                QTest.mouseClick(view.viewport(), Qt.MouseButton.RightButton, pos=point)
                self.assertTrue(wait_for(lambda: detail.call_count == 1, timeout=0.5))
                detail.assert_called_once_with(1)
                view.close()
        finally:
            QApplication.setDoubleClickInterval(previous_interval)
            connection.close()

    def test_image_right_click_waits_before_changing_the_selected_item(self):
        previous_interval = QApplication.doubleClickInterval()
        QApplication.setDoubleClickInterval(20)
        try:
            for view in (AssetGrid(), AssetTable()):
                view.resize(720, 400)
                view.show()
                view.set_items(asset_records(2, kind="image", path="missing.png"))
                view.select_item(1)
                self.app.processEvents()
                second_index = view.model().index(1, 0)
                point = (
                    view.delegate.card_rect(view.visualRect(second_index)).center()
                    if isinstance(view, AssetGrid)
                    else view.visualRect(second_index).center()
                )
                detail = Mock()
                view.detail_requested.connect(detail)

                QTest.mousePress(
                    view.viewport(), Qt.MouseButton.RightButton, pos=point
                )
                QTest.mouseRelease(
                    view.viewport(), Qt.MouseButton.RightButton, pos=point
                )

                self.assertEqual(view.selected_id, 1)
                detail.assert_called_once_with(2)
                view.close()
        finally:
            QApplication.setDoubleClickInterval(previous_interval)

    def test_right_click_requests_details_for_every_item_kind(self):
        for kind in ("image", "text", "markdown"):
            for view in (AssetGrid(), AssetTable()):
                with self.subTest(kind=kind, view=type(view).__name__):
                    view.resize(720, 400)
                    view.show()
                    view.set_items(asset_records(1, kind=kind, path="missing.png"))
                    self.app.processEvents()
                    index = view.model().index(0, 0)
                    point = (
                        view.delegate.card_rect(view.visualRect(index)).center()
                        if isinstance(view, AssetGrid)
                        else view.visualRect(index).center()
                    )
                    detail = Mock()
                    view.detail_requested.connect(detail)

                    QTest.mouseClick(
                        view.viewport(), Qt.MouseButton.RightButton, pos=point
                    )

                    detail.assert_called_once_with(1)
                    view.close()

    def test_null_thumbnail_decode_is_not_cached(self):
        path = Path(self.temp.name) / "invalid.png"
        path.write_bytes(b"not an image")
        key = widgets_module._thumbnail_cache_key(path)

        self.assertIsNotNone(key)
        self.assertIsNone(widgets_module._cache_decoded_thumbnail(key, QImage()))
        self.assertEqual(len(_THUMBNAIL_CACHE), 0)

    def test_same_size_and_mtime_replacement_invalidates_thumbnail_cache(self):
        path = Path(self.temp.name) / "replace.bmp"
        red = QImage(32, 32, QImage.Format.Format_RGB32)
        red.fill(QColor("#ff0000"))
        blue = QImage(32, 32, QImage.Format.Format_RGB32)
        blue.fill(QColor("#0000ff"))
        self.assertTrue(red.save(str(path), "BMP"))
        original_stat = path.stat()
        original_key = widgets_module._thumbnail_cache_key(path, "original-hash")
        self.assertIsNotNone(
            widgets_module._cache_decoded_thumbnail(original_key, red)
        )

        QTest.qWait(10)
        self.assertTrue(blue.save(str(path), "BMP"))
        self.assertEqual(path.stat().st_size, original_stat.st_size)
        os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

        replacement_key = widgets_module._thumbnail_cache_key(path, "replacement-hash")
        self.assertNotEqual(replacement_key, original_key)
        self.assertTrue(thumbnail_pixmap(path, "replacement-hash").isNull())

    def test_new_thumbnail_generation_can_replace_blocked_stale_decodes(self):
        decode_queue = widgets_module._ThumbnailDecodeQueue(max_workers=2, max_requests=8)
        stale_started = threading.Event()
        stale_release = threading.Event()
        current_started = threading.Event()
        started_count = 0
        started_lock = threading.Lock()
        stale_keys = [
            widgets_module._ThumbnailCacheKey(f"stale-{index}", index, 1)
            for index in range(2)
        ]
        current_key = widgets_module._ThumbnailCacheKey("current", 3, 1)

        def blocking_decode(key):
            nonlocal started_count
            if key in stale_keys:
                with started_lock:
                    started_count += 1
                    if started_count == len(stale_keys):
                        stale_started.set()
                stale_release.wait(2)
            else:
                current_started.set()
            return QImage()

        with patch("clipsave_app.widgets._decode_thumbnail_image", side_effect=blocking_decode):
            for key in stale_keys:
                self.assertTrue(decode_queue.request(key, 1))
            self.assertTrue(stale_started.wait(1))
            decode_queue.cancel_queued()
            self.assertTrue(decode_queue.request(current_key, 2))
            self.assertTrue(current_started.wait(1))
            stale_release.set()
            self.assertTrue(wait_for(lambda: decode_queue.pending_count == 0))

        self.assertTrue(decode_queue.close())

    def test_paused_thumbnail_queue_rejects_new_work_until_resumed(self):
        decode_queue = widgets_module._ThumbnailDecodeQueue(max_workers=1, max_requests=4)
        key = widgets_module._ThumbnailCacheKey("paused", 1, 1)

        self.assertTrue(decode_queue.pause_and_wait())
        self.assertFalse(decode_queue.request(key, 1))
        decode_queue.resume()
        self.assertTrue(decode_queue.request(key, 1))
        self.assertTrue(wait_for(lambda: decode_queue.pending_count == 0))
        self.assertTrue(decode_queue.close())

    def test_rapid_viewport_changes_are_coalesced_into_one_generation(self):
        grid = AssetGrid()
        grid.resize(520, 400)
        grid.show()
        self.app.processEvents()
        grid._thumbnail_refresh_timer.stop()
        initial_generation = grid._thumbnail_generation

        with patch.object(
            grid._thumbnail_loader,
            "cancel_queued",
            wraps=grid._thumbnail_loader.cancel_queued,
        ) as cancel:
            grid._thumbnail_viewport_changed(10)
            grid._thumbnail_viewport_changed(20)
            self.assertEqual(grid._thumbnail_generation, initial_generation)
            self.assertTrue(
                wait_for(lambda: grid._thumbnail_generation == initial_generation + 1)
            )

        cancel.assert_called_once_with()
        grid.close()

    def test_grid_exact_fit_width_keeps_the_last_column_on_the_first_row(self):
        grid = AssetGrid()
        grid.resize(1040, 400)
        grid.set_preview_loading_enabled(False)
        grid.set_items(asset_records(20))
        grid.show()
        self.app.processEvents()
        grid.resize(grid.width() + (-grid.viewport().width() % 4), grid.height())
        self.app.processEvents()

        self.assertEqual(grid.columns, 4)
        self.assertEqual(grid.viewport().width() % grid.columns, 0)
        first_row_top = grid.visualRect(grid.model().index(0, 0)).top()
        self.assertEqual(grid.visualRect(grid.model().index(3, 0)).top(), first_row_top)
        self.assertGreater(grid.visualRect(grid.model().index(4, 0)).top(), first_row_top)
        grid.close()

    def test_grid_reserves_scrollbar_width_without_relayout_feedback(self):
        grid = AssetGrid()
        grid.resize(1022, 600)
        grid.set_preview_loading_enabled(False)
        grid.set_items(asset_records(7))
        grid.show()

        widths = []
        ranges = []
        for _ in range(20):
            self.app.processEvents()
            widths.append(grid.viewport().width())
            ranges.append(grid.verticalScrollBar().maximum())

        self.assertEqual(
            grid.verticalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOn,
        )
        self.assertEqual(len(set(widths[-10:])), 1)
        self.assertEqual(len(set(ranges[-10:])), 1)
        grid.close()

    def test_table_keeps_fixed_row_height_without_full_resize(self):
        table = AssetTable()
        record = {
            "id": 1,
            "title": "row",
            "kind": "text",
            "tag_names": "",
            "created_at": "2026-07-12T10:00:00",
            "file_size": 3,
        }
        with patch.object(table, "resizeRowsToContents") as resize_rows:
            table.set_items([record], selected_id=1)
        resize_rows.assert_not_called()
        self.assertEqual(table.verticalHeader().defaultSectionSize(), 44)
        self.assertEqual(table.selected_id, 1)
        table.close()

    def test_large_views_keep_rows_in_models_without_linear_widget_growth(self):
        grid = AssetGrid()
        table = AssetTable()
        widget_counts = []

        for count in (1000, 5000):
            records = asset_records(count)
            grid.set_items(records, selected_id=count)
            table.set_items(records, selected_id=count)
            self.app.processEvents()

            self.assertEqual(grid.model().rowCount(), count)
            self.assertEqual(table.model().rowCount(), count)
            self.assertEqual(grid.selected_id, count)
            self.assertEqual(table.selected_id, count)
            widget_counts.append((len(grid.findChildren(QWidget)), len(table.findChildren(QWidget))))

        self.assertLessEqual(widget_counts[1][0], widget_counts[0][0] + 2)
        self.assertLessEqual(widget_counts[1][1], widget_counts[0][1] + 2)
        self.assertTrue(hasattr(grid, "favorite_requested"))
        self.assertTrue(hasattr(table, "favorite_requested"))
        grid.close()
        table.close()

    def test_views_resynchronize_visual_selection_from_selected_id_without_emitting(self):
        records = asset_records(3)
        grid = AssetGrid()
        table = AssetTable()
        grid.set_items(records, selected_id=1)
        table.set_items(records, selected_id=1)
        grid_selected = Mock()
        table_selected = Mock()
        grid.item_selected.connect(grid_selected)
        table.item_selected.connect(table_selected)

        grid.selected_id = 3
        table.selected_id = 3
        grid.sync_selection_from_selected_id()
        table.sync_selection_from_selected_id()

        self.assertEqual(grid.currentIndex().row(), 2)
        self.assertEqual(table.currentIndex().row(), 2)
        self.assertEqual([index.row() for index in grid.selectionModel().selectedIndexes()], [2])
        self.assertEqual([index.row() for index in table.selectionModel().selectedRows()], [2])
        grid_selected.assert_not_called()
        table_selected.assert_not_called()

        grid.selected_id = 99
        table.selected_id = 99
        grid.sync_selection_from_selected_id()
        table.sync_selection_from_selected_id()
        self.assertFalse(grid.currentIndex().isValid())
        self.assertFalse(table.currentIndex().isValid())
        grid.close()
        table.close()

    def test_table_formats_internal_tag_separator_for_display(self):
        table = AssetTable()
        record = asset_records(1)[0]
        record["tag_names"] = "First\x1fSecond"
        table.set_items([record])

        self.assertEqual(table.model().index(0, 2).data(), "First, Second")
        table.close()

    def test_5000_item_model_build_is_significantly_faster_than_cell_materialization(self):
        records = asset_records(5000)
        grid = AssetGrid()
        table = AssetTable()

        started = time.perf_counter()
        grid.set_items(records)
        table.set_items(records)
        model_elapsed = time.perf_counter() - started

        legacy = QTableWidget(5000, 5)
        started = time.perf_counter()
        for row, record in enumerate(records):
            for column, value in enumerate(
                (record["title"], record["kind"], record["tag_names"], record["created_at"], record["file_size"])
            ):
                legacy.setItem(row, column, QTableWidgetItem(str(value)))
        materialized_elapsed = time.perf_counter() - started

        self.assertLess(model_elapsed, materialized_elapsed * 0.5)
        self.assertLess(model_elapsed, 1.0)
        grid.close()
        table.close()
        legacy.close()

    def test_grid_loads_thumbnails_only_for_visible_items_when_enabled(self):
        path = Path(self.temp.name) / "visible.png"
        image = QImage(64, 40, QImage.Format.Format_RGB32)
        image.fill(QColor("#21a8fb"))
        self.assertTrue(image.save(str(path)))
        records = asset_records(5000, kind="image", path=str(path))
        grid = AssetGrid()
        grid.resize(520, 400)
        grid.show()
        grid.set_preview_loading_enabled(False)

        with patch.object(grid._thumbnail_loader, "request", return_value=True) as load_thumbnail:
            grid.set_items(records)
            self.app.processEvents()
            self.assertEqual(load_thumbnail.call_count, 0)

            grid.set_preview_loading_enabled(True)
            self.app.processEvents()
            visible_calls = load_thumbnail.call_count
            self.assertGreater(visible_calls, 0)
            self.assertLess(visible_calls, len(records))

            grid.hide()
            grid.set_items(records)
            self.app.processEvents()
            self.assertEqual(load_thumbnail.call_count, visible_calls)
        grid.close()

    def test_grid_delegate_preserves_selection_activation_and_favorite_signals(self):
        grid = AssetGrid()
        grid.resize(520, 400)
        grid.show()
        grid.set_items(asset_records(2))
        self.app.processEvents()
        index = grid.model().index(0, 0)
        selected = Mock()
        activated = Mock()
        favorite = Mock()
        cleared = Mock()
        grid.item_selected.connect(selected)
        grid.item_activated.connect(activated)
        grid.favorite_requested.connect(favorite)
        grid.selection_cleared.connect(cleared)

        favorite_point = grid.delegate.favorite_rect(grid.visualRect(index)).center()
        QTest.mouseClick(grid.viewport(), Qt.MouseButton.LeftButton, pos=favorite_point)
        favorite.assert_called_once_with(1, True)
        selected.assert_called_once_with(1)

        card_center = grid.delegate.card_rect(grid.visualRect(index)).center()
        QTest.mouseClick(grid.viewport(), Qt.MouseButton.LeftButton, pos=card_center)
        selected.assert_called_with(1)
        QTest.mouseDClick(grid.viewport(), Qt.MouseButton.LeftButton, pos=card_center)
        activated.assert_called_with(1)
        second_index = grid.model().index(1, 0)
        grid.setCurrentIndex(second_index)
        self.app.processEvents()
        selected.assert_called_with(2)
        second_center = grid.delegate.card_rect(grid.visualRect(second_index)).center()
        QTest.mouseClick(
            grid.viewport(),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ControlModifier,
            pos=second_center,
        )
        self.app.processEvents()
        cleared.assert_called_once_with()
        self.assertIsNone(grid.selected_id)
        grid.close()

    def test_image_mouse_gestures_are_consistent_in_grid_and_table(self):
        path = Path(self.temp.name) / "gesture.png"
        image = QImage(64, 40, QImage.Format.Format_RGB32)
        image.fill(QColor("#21a8fb"))
        self.assertTrue(image.save(str(path)))

        previous_interval = QApplication.doubleClickInterval()
        QApplication.setDoubleClickInterval(80)
        try:
            for view in (AssetGrid(), AssetTable()):
                view.resize(720, 400)
                view.show()
                view.set_items(asset_records(1, kind="image", path=str(path)))
                self.app.processEvents()

                activated = Mock()
                detail = Mock()
                opened = Mock()
                view.item_activated.connect(activated)
                view.detail_requested.connect(detail)
                view.open_requested.connect(opened)
                index = view.model().index(0, 0)
                point = (
                    view.delegate.card_rect(view.visualRect(index)).center()
                    if isinstance(view, AssetGrid)
                    else view.visualRect(index).center()
                )

                QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
                QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
                self.app.processEvents()
                activated.assert_called_once_with(1)
                detail.assert_not_called()
                opened.assert_not_called()

                activated.reset_mock()
                QTest.mouseClick(view.viewport(), Qt.MouseButton.RightButton, pos=point)
                self.assertTrue(wait_for(lambda: detail.call_count == 1, timeout=0.7))
                detail.assert_called_once_with(1)
                opened.assert_not_called()

                detail.reset_mock()
                QTest.mouseClick(view.viewport(), Qt.MouseButton.RightButton, pos=point)
                QTest.mouseClick(view.viewport(), Qt.MouseButton.RightButton, pos=point)
                self.app.processEvents()
                opened.assert_not_called()
                self.assertEqual(detail.call_count, 2)
                detail.assert_called_with(1)

                activated.reset_mock()
                detail.reset_mock()
                QTest.qWait(100)
                QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
                QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
                QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
                self.app.processEvents()
                opened.assert_called_once_with(1)
                activated.assert_called_once_with(1)
                QTest.qWait(100)
                activated.assert_called_once_with(1)
                view.close()
        finally:
            QApplication.setDoubleClickInterval(previous_interval)

    def test_text_double_click_activates_and_third_click_opens_reader(self):
        previous_interval = QApplication.doubleClickInterval()
        QApplication.setDoubleClickInterval(80)
        try:
            for view in (AssetGrid(), AssetTable()):
                with self.subTest(view=type(view).__name__):
                    view.resize(720, 400)
                    view.show()
                    view.set_items(asset_records(1, kind="text"))
                    self.app.processEvents()
                    activated = Mock()
                    opened = Mock()
                    view.item_activated.connect(activated)
                    view.open_requested.connect(opened)
                    index = view.model().index(0, 0)
                    point = (
                        view.delegate.card_rect(view.visualRect(index)).center()
                        if isinstance(view, AssetGrid)
                        else view.visualRect(index).center()
                    )

                    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
                    QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
                    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
                    self.app.processEvents()

                    activated.assert_called_once_with(1)
                    opened.assert_called_once_with(1)
                    view.close()
        finally:
            QApplication.setDoubleClickInterval(previous_interval)

    def test_text_dialog_displays_plain_text_without_markdown_rendering(self):
        content = "# literal heading\n**literal emphasis**"
        dialog = TextDialog("Copied text", content)
        try:
            self.assertTrue(dialog.property("textDialog"))
            self.assertEqual(dialog.browser.toPlainText(), content)
        finally:
            dialog.close()

    def test_table_favorite_control_emits_requested_change(self):
        table = AssetTable()
        table.resize(720, 300)
        table.show()
        table.set_items(asset_records(2))
        self.app.processEvents()
        index = table.model().index(0, 0)
        selected = Mock()
        favorite = Mock()
        table.item_selected.connect(selected)
        table.favorite_requested.connect(favorite)

        point = table._favorite_delegate.favorite_rect(table.visualRect(index)).center()
        QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=point)

        favorite.assert_called_once_with(1, True)
        selected.assert_called_with(1)
        table.close()

    def test_table_favorite_delegate_keeps_title_out_of_style_paint(self):
        class RecordingStyle(QProxyStyle):
            def __init__(self):
                super().__init__()
                self.item_texts = []

            def drawControl(self, element, option, painter, widget=None):
                if element == QStyle.ControlElement.CE_ItemViewItem:
                    self.item_texts.append(option.text)
                super().drawControl(element, option, painter, widget)

        table = AssetTable()
        style = RecordingStyle()
        table.setStyle(style)
        table.set_items(asset_records(1))
        canvas = QImage(400, 44, QImage.Format.Format_ARGB32)
        canvas.fill(Qt.GlobalColor.transparent)
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 400, 44)
        option.widget = table
        painter = QPainter(canvas)

        table._favorite_delegate.paint(painter, option, table.model().index(0, 0))

        painter.end()
        self.assertEqual(style.item_texts, [""])
        table.close()

    def test_detail_clears_preview_and_updates_image_only_buttons(self):
        image_path = Path(self.temp.name) / "preview.png"
        image = QImage(64, 40, QImage.Format.Format_RGB32)
        image.fill(QColor("#21a8fb"))
        self.assertTrue(image.save(str(image_path)))
        base = {
            "id": 1,
            "title": "preview",
            "content": "",
            "width": 64,
            "height": 40,
            "file_size": image_path.stat().st_size,
            "created_at": "2026-07-12T10:00:00",
            "source": "test",
            "collection_id": None,
            "tag_names": ("tag_" + "x" * 120) + "\x1f" + ("tag_" + "y" * 120),
            "tag_colors": "#21a8fb\x1f#20a464",
            "ai_description": "A" * 300,
            "ocr_text": "B" * 300,
            "notes": "",
            "favorite": 0,
        }
        image_item = dict(base, kind="image", path=str(image_path))
        text_item = dict(base, id=2, kind="text", path=None, content="plain text", width=0, height=0)
        panel = DetailPanel()

        panel.set_item(image_item)
        self.assertTrue(wait_for(lambda: not panel.image_preview.pixmap().isNull()))
        self.assertFalse(panel.image_preview.pixmap().isNull())
        self.assertTrue(panel.ai_button.isEnabled())
        self.assertTrue(panel.ocr_button.isEnabled())

        panel.set_item(text_item)
        self.assertTrue(panel.image_preview.pixmap().isNull())
        self.assertEqual(panel.text_preview.toPlainText(), "plain text")
        self.assertFalse(panel.ai_button.isEnabled())
        self.assertFalse(panel.ocr_button.isEnabled())

        panel.set_item(image_item)
        self.assertEqual(panel.text_preview.toPlainText(), "")
        panel.clear_item()
        self.assertIsNone(panel.current_item)
        self.assertTrue(all(not button.isEnabled() for button in panel.item_action_buttons))
        self.assertFalse(panel.notes.isEnabled())
        self.assertFalse(panel.collection_combo.isEnabled())
        self.assertFalse(panel.add_tag_button.isEnabled())
        panel.close()

    def test_detail_tags_offer_more_entry_without_losing_hidden_tags(self):
        panel = DetailPanel()
        item = {
            "id": 1,
            "title": "tagged",
            "kind": "text",
            "path": None,
            "content": "text",
            "width": 0,
            "height": 0,
            "file_size": 4,
            "created_at": "2026-07-13T01:58:02",
            "source": "clipboard",
            "collection_id": None,
            "tag_names": "one\x1ftwo\x1fthree\x1ffour\x1ffive\x1fsix",
            "tag_colors": "#111111\x1f#222222\x1f#333333\x1f#444444\x1f#555555\x1f#666666",
            "ai_description": "",
            "ocr_text": "",
            "notes": "",
            "favorite": 0,
        }

        panel.set_item(item)
        self.assertIsNotNone(panel.tags_more_button)
        self.assertIn("+2", panel.tags_more_button.text())
        panel.tags_more_button.click()
        self.assertEqual(
            [
                panel.tags_box.itemAt(index).widget().text()
                for index in range(panel.tags_box.count())
                if panel.tags_box.itemAt(index).widget().objectName() == "TagChip"
            ],
            ["one", "two", "three", "four", "five", "six"],
        )
        self.assertEqual(panel.tags_more_button.text(), "收起标签")
        panel.close()

    def test_detail_panel_scrolls_in_a_small_work_area(self):
        panel = DetailPanel()
        self.assertEqual(panel.maximumWidth(), 340)
        panel.resize(340, 300)
        panel.show()
        self.assertTrue(wait_for(lambda: panel.verticalScrollBar().maximum() > 0))
        self.assertGreater(panel.content_widget.sizeHint().height(), panel.viewport().height())
        scroll_bar = panel.verticalScrollBar()
        scroll_bar.set_active(True)
        self.assertEqual(scroll_bar._handle_rect().right(), scroll_bar.rect().right())
        panel.close()

    def test_detail_long_filename_and_path_do_not_expand_panel(self):
        image_path = Path(self.temp.name) / ("wide_" + "capture_" * 8 + ".png")
        image = QImage(1600, 900, QImage.Format.Format_RGB32)
        image.fill(QColor("#21a8fb"))
        self.assertTrue(image.save(str(image_path)))
        panel = DetailPanel()
        panel.resize(340, 640)
        panel.show()
        item = {
            "id": 1,
            "title": image_path.name,
            "kind": "image",
            "path": str(image_path),
            "content": "",
            "width": 100,
            "height": 100,
            "file_size": 1024,
            "created_at": "2026-07-13T01:58:02",
            "source": "clipboard",
            "collection_id": None,
            "tag_names": "",
            "tag_colors": "",
            "ai_description": "",
            "ocr_text": "",
            "notes": "",
            "favorite": 0,
        }

        panel.set_item(item)
        self.assertTrue(wait_for(lambda: not panel.image_preview.pixmap().isNull()))

        self.assertNotIn("\u200b", panel.title.text())
        self.assertNotIn("\u200b", panel.meta.text())
        self.assertEqual(panel.title.toolTip(), item["title"])
        self.assertEqual(panel.meta.toolTip(), item["path"])
        self.assertLessEqual(panel.content_widget.width(), panel.viewport().width())
        self.assertLessEqual(panel.image_preview.pixmap().width(), panel.image_preview.width())
        self.assertEqual(panel.horizontalScrollBar().maximum(), 0)
        panel.resize(280, 640)
        self.assertTrue(
            wait_for(
                lambda: panel.image_preview.pixmap().width()
                <= panel.image_preview.width()
            )
        )
        panel.close()

    def test_settings_default_size_does_not_scroll(self):
        settings = Mock()
        settings.get.side_effect = lambda _key, default=None: default
        dialog = SettingsDialog(settings)
        self.assertEqual(dialog.size(), QSize(720, 600))
        self.assertEqual(dialog.layout().contentsMargins().left(), 1)
        self.assertTrue(dialog.property("settingsDialog"))
        self.assertTrue(dialog.follow_system_theme.isChecked())
        self.assertFalse(dialog.start_with_windows.isChecked())
        self.assertFalse(dialog.dark_theme_switch.isEnabled())
        dialog.follow_system_theme.setChecked(False)
        self.assertTrue(dialog.dark_theme_switch.isEnabled())
        dialog.show()
        self.app.processEvents()
        scroll = dialog.findChild(QScrollArea, "DialogScroll")
        self.assertIsNone(scroll)
        content = dialog.findChild(QWidget, "DialogContent")
        self.assertIsNotNone(content)
        self.assertLessEqual(content.sizeHint().height(), content.height())
        self.assertFalse(hasattr(dialog, "embedding_model"))
        self.assertNotIn(
            "向量",
            " ".join(label.text() for label in dialog.findChildren(QLabel)),
        )
        dialog.close()

    def test_dialogs_fit_small_available_screen_without_unbounded_geometry(self):
        screen = Mock()
        screen.availableGeometry.return_value = QRect(0, 0, 500, 400)
        settings = Mock()
        settings.get.side_effect = lambda _key, default=None: default

        with patch.object(SettingsDialog, "screen", return_value=screen):
            settings_dialog = SettingsDialog(settings)
        self.assertLessEqual(settings_dialog.width(), 468)
        self.assertLessEqual(settings_dialog.height(), 368)
        self.assertIsNotNone(settings_dialog.findChild(QScrollArea, "DialogScroll"))

        with patch.object(DateDialog, "screen", return_value=screen):
            date_dialog = DateDialog([])
        self.assertLessEqual(date_dialog.width(), 420)
        self.assertLessEqual(date_dialog.height(), 368)

        with patch.object(FluentMessageDialog, "screen", return_value=screen):
            message_dialog = FluentMessageDialog("提示", "内容" * 100)
        self.assertLessEqual(message_dialog.width(), 468)
        self.assertLessEqual(message_dialog.height(), 368)

        settings_dialog.close()
        date_dialog.close()
        message_dialog.close()

    def test_sidebar_brand_divider_matches_toolbar_height(self):
        sidebar = Sidebar()
        sidebar.resize(242, 700)
        sidebar.show()
        self.app.processEvents()

        divider = sidebar.brand_divider_rect()
        self.assertEqual(divider.y(), Sidebar.BRAND_AREA_HEIGHT - 1)
        self.assertEqual(divider.width(), sidebar.width())
        self.assertIsNone(sidebar.findChild(QFrame, "SidebarBrandDivider"))
        self.assertTrue(sidebar.collapse_button.prominent_icon)
        self.assertFalse(sidebar.collapse_button.icon().isNull())
        sidebar.close()

    def test_application_icon_contains_multiple_raster_sizes(self):
        sizes = {(size.width(), size.height()) for size in create_app_icon().availableSizes()}
        self.assertTrue({(32, 32), (64, 64), (128, 128), (256, 256)}.issubset(sizes))

    def test_icon_controls_have_accessible_names(self):
        button = widgets_module.IconButton("close", "Close details")
        status = widgets_module.CaptureStatusButton()
        self.assertEqual(button.accessibleName(), "Close details")
        self.assertTrue(status.accessibleName())
        button.close()
        status.close()

    def test_collapsed_navigation_keeps_accessible_names(self):
        sidebar = widgets_module.Sidebar()
        sidebar.set_primary({"all": 1})
        sidebar.set_collapsed(True, animate=False)
        self.assertTrue(sidebar.nav_buttons)
        for button in sidebar.nav_buttons.values():
            self.assertEqual(button.accessibleName(), button.label)
        sidebar.close()

    def test_stale_grid_generation_does_not_cache_completed_decode(self):
        path = Path(self.temp.name) / "stale.png"
        image = QImage(64, 40, QImage.Format.Format_RGB32)
        image.fill(QColor("#21a8fb"))
        self.assertTrue(image.save(str(path)))
        started = threading.Event()
        release = threading.Event()
        decode_thumbnail = widgets_module._decode_thumbnail_image

        def delayed_decode(key):
            started.set()
            release.wait(2.0)
            return decode_thumbnail(key)

        grid = AssetGrid()
        grid.resize(520, 400)
        grid.show()
        try:
            with patch("clipsave_app.widgets._decode_thumbnail_image", side_effect=delayed_decode):
                grid.set_items(asset_records(1, kind="image", path=str(path)))
                self.app.processEvents()
                self.assertTrue(started.wait(1.0))
                grid.set_items([])
                release.set()
                self.assertTrue(wait_for(lambda: grid._thumbnail_loader.pending_count == 0))
            self.assertEqual(len(_THUMBNAIL_CACHE), 0)
        finally:
            release.set()
            grid.close()

    def test_file_change_replaces_same_path_cache_entry(self):
        path = Path(self.temp.name) / "replace.png"
        first_image = QImage(64, 40, QImage.Format.Format_RGB32)
        first_image.fill(QColor("#21a8fb"))
        self.assertTrue(first_image.save(str(path)))
        grid = AssetGrid()
        grid.resize(520, 400)
        grid.show()
        grid.set_items(asset_records(1, kind="image", path=str(path)))
        self.assertTrue(wait_for(lambda: not thumbnail_pixmap(path).isNull()))
        first_cache_key = thumbnail_pixmap(path).cacheKey()

        second_image = QImage(90, 55, QImage.Format.Format_RGB32)
        second_image.fill(QColor("#f04f5f"))
        self.assertTrue(second_image.save(str(path)))
        stat = path.stat()
        changed_ns = max(time.time_ns(), stat.st_mtime_ns + 1_000_000)
        os.utime(path, ns=(changed_ns, changed_ns))
        records = asset_records(1, kind="image", path=str(path))
        records[0]["file_size"] = path.stat().st_size
        records[0]["width"] = 90
        records[0]["height"] = 55
        grid.set_items(records)

        self.assertTrue(
            wait_for(
                lambda: not thumbnail_pixmap(path).isNull()
                and thumbnail_pixmap(path).cacheKey() != first_cache_key
            )
        )
        self.assertEqual(len(_THUMBNAIL_CACHE), 1)
        grid.close()

    def test_thumbnail_queue_and_cache_are_bounded(self):
        started = threading.Event()
        release = threading.Event()

        def delayed_decode(_key):
            started.set()
            release.wait(2.0)
            return QImage()

        queue = widgets_module._ThumbnailDecodeQueue(max_workers=1, max_requests=3)
        capacity_events = []
        queue.capacity_available.connect(lambda: capacity_events.append(True))
        keys = [
            widgets_module._ThumbnailCacheKey(str(Path(self.temp.name) / f"{index}.png"), index, index)
            for index in range(5)
        ]
        try:
            with patch("clipsave_app.widgets._decode_thumbnail_image", side_effect=delayed_decode):
                accepted = [queue.request(key, 1) for key in keys]
                self.assertEqual(accepted, [True, True, True, False, False])
                self.assertTrue(started.wait(1.0))
                self.assertEqual(queue.pending_count, 3)
                self.assertLessEqual(queue.queued_count, 2)
                release.set()
                self.assertTrue(wait_for(lambda: queue.pending_count == 0))
                self.assertTrue(capacity_events)
        finally:
            release.set()
            queue.close()

        original_limit = _THUMBNAIL_CACHE.limit
        _THUMBNAIL_CACHE.limit = 3
        try:
            for index in range(5):
                key = widgets_module._ThumbnailCacheKey(f"cache-{index}", index, index)
                _THUMBNAIL_CACHE[key] = QPixmap(1, 1)
            self.assertEqual(len(_THUMBNAIL_CACHE), 3)
        finally:
            _THUMBNAIL_CACHE.limit = original_limit


    def test_sidebar_reuses_one_animation_without_expand_width_jump(self):
        sidebar = Sidebar()
        started = []
        finished = []
        sidebar.width_animation_started.connect(lambda: started.append(True))
        sidebar.width_animation_finished.connect(lambda: finished.append(True))
        sidebar.set_collapsed(True, animate=False)
        sidebar.set_collapsed(False, animate=True)

        self.assertEqual(sidebar.minimumWidth(), 72)
        self.assertEqual(len(sidebar.findChildren(QPropertyAnimation)), 1)
        self.assertTrue(wait_for(lambda: sidebar.minimumWidth() == 200))
        self.assertEqual(started, [True])
        self.assertEqual(finished, [True])
        sidebar.set_collapsed(True, animate=True)
        self.assertEqual(len(sidebar.findChildren(QPropertyAnimation)), 1)
        sidebar.close()

    def test_grid_defers_layout_recalculation_during_sidebar_animation(self):
        grid = AssetGrid()
        grid.resize(600, 400)
        grid.show()
        self.app.processEvents()

        with patch.object(grid, "_update_grid_size") as update_grid_size:
            grid.set_layout_updates_suspended(True)
            grid.resize(760, 400)
            self.app.processEvents()
            update_grid_size.assert_not_called()

            grid.set_layout_updates_suspended(False)
            update_grid_size.assert_called_once_with()

        grid.close()

    def test_large_markdown_uses_plain_text_instead_of_blocking_rich_parse(self):
        content = "# heading\n" + ("x" * (MAX_RICH_MARKDOWN_BYTES + 1))
        with patch.object(_SafeMarkdownBrowser, "setMarkdown") as set_markdown, patch.object(
            _SafeMarkdownBrowser, "setPlainText"
        ) as set_plain_text:
            dialog = MarkdownDialog("Large", content)

        set_plain_text.assert_called_once_with(content)
        set_markdown.assert_not_called()
        dialog.close()


class WidgetSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_sidebar_tag_color_icon_survives_active_state_refresh(self):
        sidebar = Sidebar()
        sidebar.set_tags([{"id": 7, "name": "Red", "amount": 1, "color": "#ff0000"}])
        button = sidebar.tag_buttons[0]
        color_icon_key = button.icon().cacheKey()

        sidebar.set_active("tag:7")
        self.assertEqual(button.icon().cacheKey(), color_icon_key)
        sidebar.set_active("all")
        self.assertEqual(button.icon().cacheKey(), color_icon_key)
        sidebar.close()

    def test_sidebar_tags_created_while_collapsed_stay_collapsed(self):
        sidebar = Sidebar()
        sidebar.set_collapsed(True, animate=False)
        sidebar.set_tags([{"id": 7, "name": "Red", "amount": 1, "color": "#ff0000"}])

        self.assertTrue(sidebar.tag_buttons[0].collapsed)
        self.assertEqual(sidebar.tag_buttons[0].text(), "")
        sidebar.close()

    def test_sidebar_delete_buttons_align_with_add_buttons_and_hide_when_collapsed(self):
        sidebar = Sidebar()
        sidebar.resize(242, 720)
        sidebar.set_primary({"all": 2, "favorite": 1, "image": 1, "text": 1, "markdown": 0})
        sidebar.set_collections([{"id": 3, "name": "Work", "amount": 2}])
        sidebar.set_tags([{"id": 7, "name": "Red", "amount": 1, "color": "#ff0000"}])
        deleted_collections = []
        deleted_tags = []
        sidebar.delete_collection_requested.connect(
            lambda ident, name: deleted_collections.append((ident, name))
        )
        sidebar.delete_tag_requested.connect(
            lambda ident, name: deleted_tags.append((ident, name))
        )
        sidebar.show()
        self.app.processEvents()

        collection_delete = sidebar.collection_delete_buttons[3]
        tag_delete = sidebar.tag_delete_buttons[7]
        collection_add_x = sidebar.collection_heading.add_button.mapTo(
            sidebar, sidebar.collection_heading.add_button.rect().center()
        ).x()
        tag_add_x = sidebar.tag_heading.add_button.mapTo(
            sidebar, sidebar.tag_heading.add_button.rect().center()
        ).x()
        self.assertEqual(
            collection_delete.mapTo(sidebar, collection_delete.rect().center()).x(),
            collection_add_x,
        )
        self.assertEqual(
            tag_delete.mapTo(sidebar, tag_delete.rect().center()).x(),
            tag_add_x,
        )

        collection_delete.click()
        tag_delete.click()
        self.assertEqual(deleted_collections, [(3, "Work")])
        self.assertEqual(deleted_tags, [(7, "Red")])

        sidebar.set_collapsed(True, animate=False)
        self.app.processEvents()
        self.assertTrue(collection_delete.isHidden())
        self.assertTrue(tag_delete.isHidden())
        primary_button = sidebar.nav_buttons["all"]
        collection_button = sidebar.collection_buttons[0]
        tag_button = sidebar.tag_buttons[0]
        primary_left = primary_button.mapTo(sidebar, QPoint(0, 0)).x()
        self.assertEqual(collection_button.mapTo(sidebar, QPoint(0, 0)).x(), primary_left)
        self.assertEqual(tag_button.mapTo(sidebar, QPoint(0, 0)).x(), primary_left)
        self.assertEqual(collection_button.width(), primary_button.width())
        self.assertEqual(tag_button.width(), primary_button.width())
        sidebar.close()

    def test_sidebar_large_classifications_scroll_and_offer_more_tags(self):
        sidebar = Sidebar()
        sidebar.resize(242, 360)
        sidebar.set_primary({"all": 1})
        sidebar.set_collections(
            [{"id": index, "name": f"Collection {index}", "amount": index} for index in range(20)]
        )
        sidebar.set_tags(
            [{"id": 100 + index, "name": f"Tag {index}", "amount": index, "color": "#21a8fb"} for index in range(10)]
        )
        sidebar.show()
        self.app.processEvents()

        self.assertGreater(sidebar.classification_scroll.verticalScrollBar().maximum(), 0)
        self.assertIsNotNone(sidebar.tags_more_button)
        self.assertIn("2", sidebar.tags_more_button.text())
        sidebar.tags_more_button.click()
        self.assertEqual(len(sidebar.tag_delete_buttons), 10)
        self.assertIn("收起标签", sidebar.tags_more_button.text())
        sidebar.tags_more_button.click()
        self.assertEqual(len(sidebar.tag_delete_buttons), 8)
        sidebar.close()

    def test_sidebar_collapse_only_renders_the_changed_collapse_icon(self):
        sidebar = Sidebar()
        sidebar.set_primary({"all": 2, "favorite": 1, "image": 1, "text": 1, "markdown": 0})
        sidebar.set_collections([{"id": 3, "name": "Work", "amount": 2}])
        sidebar.set_tags([{"id": 7, "name": "Red", "amount": 1, "color": "#ff0000"}])

        with patch.object(
            widgets_module, "lucide_icon", wraps=widgets_module.lucide_icon
        ) as render_icon:
            sidebar.set_collapsed(True, animate=False)
            sidebar.set_collapsed(False, animate=False)

        self.assertEqual(render_icon.call_count, 2)
        sidebar.close()

    def test_table_clear_selection_also_clears_current_index(self):
        table = AssetTable()
        table.set_items(asset_records(2), selected_id=2)
        self.assertTrue(table.currentIndex().isValid())

        table.clear_selected_item()

        self.assertFalse(table.currentIndex().isValid())
        table.close()

    def test_markdown_browsers_block_local_unc_and_network_resources(self):
        browser = _SafeMarkdownBrowser()
        blocked_urls = (
            "relative.png",
            "C:/Users/example/private.png",
            "file:///C:/Users/example/private.png",
            r"\\server\share\private.png",
            "//server/share/private.png",
            "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
            "https://example.com/tracker.png",
            "ftp://example.com/private.png",
        )

        for value in blocked_urls:
            with self.subTest(value=value):
                resource = browser.loadResource(QTextDocument.ResourceType.ImageResource, QUrl(value))
                self.assertIsInstance(resource, QByteArray)
                self.assertTrue(resource.isEmpty())

        dialog = MarkdownDialog("Example", "![private](relative.png)", r"C:\notes\example.md")
        panel = DetailPanel()
        self.assertTrue(dialog.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertEqual(dialog.objectName(), "FluentDialog")
        self.assertTrue(dialog.property("markdownDialog"))
        self.assertEqual(dialog.layout().contentsMargins().left(), 1)
        self.assertEqual(dialog.browser.searchPaths(), [])
        self.assertIsInstance(panel.text_preview, _SafeMarkdownBrowser)
        for markdown_browser in (browser, dialog.browser, panel.text_preview):
            self.assertEqual(
                markdown_browser.horizontalScrollBarPolicy(),
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
            )
            self.assertEqual(
                markdown_browser.verticalScrollBarPolicy(),
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
            )
        dialog.close()
        panel.close()
        browser.close()

    def test_text_context_menu_uses_theme_aware_icons(self):
        previous_dark_theme = self.app.property("darkTheme")
        self.app.setProperty("darkTheme", True)
        browser = _SafeMarkdownBrowser()
        browser.setPlainText("selected text")
        browser.selectAll()
        menu = browser._create_themed_context_menu()
        try:
            actions = {action.objectName(): action for action in menu.actions()}
            self.assertFalse(actions["edit-copy"].icon().isNull())
            self.assertFalse(actions["select-all"].icon().isNull())
            copy_icon = actions["edit-copy"].icon().pixmap(16, 16).toImage()
            visible_colors = [
                copy_icon.pixelColor(x, y)
                for x in range(copy_icon.width())
                for y in range(copy_icon.height())
                if copy_icon.pixelColor(x, y).alpha() > 0
            ]
            self.assertTrue(visible_colors)
            self.assertGreater(max(color.red() for color in visible_colors), 180)
        finally:
            menu.close()
            browser.close()
            self.app.setProperty("darkTheme", previous_dark_theme)

    def test_failed_settings_save_restores_previous_data(self):
        settings = Mock()
        settings.data = {
            "close_to_tray": True,
            "ai_base_url": "https://old.example/v1",
            "custom": "preserve",
        }
        settings.get.side_effect = lambda key, default=None: settings.data.get(key, default)
        settings.update.side_effect = OSError("disk full")
        dialog = SettingsDialog(settings)
        dialog.base_url.setText("https://new.example/v1")

        with patch("clipsave_app.widgets.QMessageBox.warning") as warning:
            dialog.accept()

        self.assertEqual(
            settings.data,
            {
                "close_to_tray": True,
                "ai_base_url": "https://old.example/v1",
                "custom": "preserve",
            },
        )
        settings.update.assert_called_once()
        warning.assert_called_once()
        self.assertEqual(dialog.result(), 0)
        dialog.close()

    def test_startfile_failure_is_reported_to_the_user(self):
        parent = QWidget()
        with (
            patch("clipsave_app.widgets.os.startfile", side_effect=OSError("no association")) as startfile,
            patch("clipsave_app.widgets.QMessageBox.warning") as warning,
        ):
            _startfile_or_warn(parent, Path("missing.md"))

        startfile.assert_called_once_with("missing.md")
        warning.assert_called_once()
        parent.close()


if __name__ == "__main__":
    unittest.main()
