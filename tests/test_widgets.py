import os
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThread, Qt
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QTableWidget, QTableWidgetItem, QWidget

import clipsave_app.widgets as widgets_module
from clipsave_app.widgets import AssetGrid, AssetTable, DetailPanel, _THUMBNAIL_CACHE, thumbnail_pixmap


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

    def test_grid_pauses_hidden_preview_queue_and_uses_deque(self):
        grid = AssetGrid()
        grid.show()
        self.app.processEvents()
        card = Mock()
        grid.pending_previews.append(card)
        self.assertIsInstance(grid.pending_previews, deque)

        grid.set_preview_loading_enabled(False)
        grid._load_next_preview()
        card.load_preview.assert_not_called()
        self.assertEqual(len(grid.pending_previews), 1)

        grid.set_preview_loading_enabled(True)
        grid._load_next_preview()
        card.load_preview.assert_called_once()
        grid.hide()
        grid.set_items([])
        self.assertTrue(grid.rebuild_pending)
        self.assertFalse(grid.rebuild_timer.isActive())
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
            self.assertEqual(len(grid.cards), 0)
            widget_counts.append((len(grid.findChildren(QWidget)), len(table.findChildren(QWidget))))

        self.assertLessEqual(widget_counts[1][0], widget_counts[0][0] + 2)
        self.assertLessEqual(widget_counts[1][1], widget_counts[0][1] + 2)
        self.assertTrue(hasattr(grid, "favorite_requested"))
        self.assertTrue(hasattr(table, "favorite_requested"))
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
        grid.item_selected.connect(selected)
        grid.item_activated.connect(activated)
        grid.favorite_requested.connect(favorite)

        favorite_point = grid.delegate.favorite_rect(grid.visualRect(index)).center()
        QTest.mouseClick(grid.viewport(), Qt.MouseButton.LeftButton, pos=favorite_point)
        favorite.assert_called_once_with(1, True)
        selected.assert_not_called()

        card_center = grid.delegate.card_rect(grid.visualRect(index)).center()
        QTest.mouseClick(grid.viewport(), Qt.MouseButton.LeftButton, pos=card_center)
        selected.assert_called_with(1)
        QTest.mouseDClick(grid.viewport(), Qt.MouseButton.LeftButton, pos=card_center)
        activated.assert_called_with(1)
        grid.close()

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
            "tag_names": "",
            "tag_colors": "",
            "ai_description": "",
            "ocr_text": "",
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
        panel.close()

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


if __name__ == "__main__":
    unittest.main()
