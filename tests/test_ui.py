import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QMessageBox

from clipsave_app.app import create_app_icon
from clipsave_app.database import LibraryDatabase
from clipsave_app.main_window import AsyncSignals, MainWindow
from clipsave_app.settings import Settings
from clipsave_app.widgets import DateDialog, DraggableBar, SettingsDialog


class MainWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.database = LibraryDatabase(root / "test.db")
        self.database.add_text("first clipboard item")
        self.settings = Settings(root / "settings.json")
        self.settings.set("monitoring", False)
        self.window = MainWindow(self.database, self.settings, create_app_icon(), scan_on_start=False)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.force_quit = True
        self.window.close()
        self.temp.cleanup()

    def test_panels_and_navigation(self):
        self.assertEqual(self.window.windowTitle(), "")
        self.assertTrue(self.window.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertEqual(self.window.window_title_bar.height(), 32)
        self.assertEqual(self.window.brand_label.text, "ClipSave")
        self.assertEqual(self.window.brand_label.objectName(), "BrandTitle")
        self.assertEqual(self.window.brand_label.geometry().width(), 200)
        self.assertEqual(self.window.brand_label.geometry().height(), 86)
        self.assertEqual(self.window.brand_label.vertical_scale, 0.9)
        self.window.window_title_bar.maximize_button.click()
        self.app.processEvents()
        self.assertTrue(self.window.isMaximized())
        self.window.window_title_bar.maximize_button.click()
        self.app.processEvents()
        self.assertFalse(self.window.isMaximized())
        self.assertFalse(hasattr(self.window, "capture_state"))
        self.assertFalse(hasattr(self.window.sidebar, "brand_icon"))
        self.assertFalse(hasattr(self.window.sidebar, "brand"))
        self.assertTrue(self.window.detail.testAttribute(Qt.WidgetAttribute.WA_StyledBackground))
        self.assertEqual(self.window.library_header.height(), 44)
        self.assertIsInstance(self.window.top_bar, DraggableBar)
        self.assertEqual(len(self.window.resize_handles), 8)
        self.assertEqual(self.window.resize_handles["right"].geometry().width(), 6)
        scroll_bar = self.window.grid.verticalScrollBar()
        self.assertEqual(scroll_bar.objectName(), "AutoHideScrollBar")
        self.assertFalse(bool(scroll_bar.property("active")))
        scroll_bar.setRange(0, 100)
        scroll_bar.setValue(1)
        self.assertTrue(bool(scroll_bar.property("active")))
        self.assertEqual(self.window.sidebar.collapse_button.text().strip(), "收起侧栏")
        self.assertFalse(self.window.detail.isVisible())
        self.window.sidebar.set_collapsed(True, animate=False)
        self.assertTrue(self.window.sidebar.collapsed)
        self.assertEqual(self.window.sidebar.collapse_button.text(), "")
        self.assertEqual(self.window.sidebar.collapse_button.toolTip(), "展开侧栏")
        self.window.sidebar.set_collapsed(False, animate=False)
        self.assertEqual(self.window.sidebar.collapse_button.text().strip(), "收起侧栏")

        self.window.open_sort_menu()
        self.app.processEvents()
        self.assertIsNotNone(self.window.sort_menu)
        self.assertTrue(self.window.sort_menu.isVisible())
        self.window.open_sort_menu()
        self.app.processEvents()
        self.assertIsNone(self.window.sort_menu)
        self.window.open_day(self.database.days()[0][0])
        self.assertEqual(len(self.window.current_items), 1)
        self.window.select_item(self.window.current_items[0]["id"])
        self.window.toggle_detail()
        self.app.processEvents()
        self.assertTrue(self.window.detail.isVisible())

        dialog = SettingsDialog(self.settings, self.window)
        self.assertTrue(dialog.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertEqual(dialog.objectName(), "FluentDialog")
        self.assertEqual(dialog.import_button.text(), "导入文件")
        import_requests = []
        dialog.import_requested.connect(lambda: import_requests.append(True))
        dialog.import_button.click()
        self.assertEqual(import_requests, [True])
        dialog.close()

        date_dialog = DateDialog(self.database.days(), self.window)
        self.assertTrue(date_dialog.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertEqual(date_dialog.objectName(), "FluentDialog")
        date_dialog.close()
        self.assertFalse(self.window.table.showGrid())
        self.assertTrue(self.window.table.alternatingRowColors())

    def test_filter_clears_hidden_selection_and_only_refreshes_visible_view(self):
        first_id = self.window.current_items[0]["id"]
        second_id = self.database.add_text("second clipboard item")
        self.window.refresh_items()
        self.window.select_item(first_id)
        self.window.toggle_detail()
        self.assertEqual(self.window.detail.current_item["id"], first_id)

        with patch.object(self.window.grid, "set_items", wraps=self.window.grid.set_items) as grid_set, patch.object(
            self.window.table, "set_items", wraps=self.window.table.set_items
        ) as table_set:
            self.window.search.setText("second clipboard")
            self.window.search_timer.stop()
            self.window.refresh_items()
            self.assertEqual([item["id"] for item in self.window.current_items], [second_id])
            self.assertIsNone(self.window.current_item_id)
            self.assertIsNone(self.window.detail.current_item)
            self.assertIsNotNone(self.database.get_item(first_id))
            grid_set.assert_called_once()
            table_set.assert_not_called()

            self.window.set_view_mode("list")
            table_set.assert_called_once()
            self.assertFalse(self.window.grid.preview_loading_enabled)

    def test_stale_async_results_do_not_replace_current_detail(self):
        first_id = self.window.current_items[0]["id"]
        second_id = self.database.add_text("second clipboard item")
        self.window.refresh_items()
        self.window.select_item(second_id)
        self.window.toggle_detail()

        stale_token, stale_signals = object(), AsyncSignals()
        active_token, active_signals = object(), AsyncSignals()
        self.window._ai_requests[first_id] = (active_token, active_signals)
        self.window._async_signals.update((stale_signals, active_signals))
        self.window._ai_succeeded(stale_token, stale_signals, first_id, "stale description", [1.0])
        self.assertEqual(self.database.get_item(first_id)["ai_description"], "")
        self.assertEqual(self.window.detail.current_item["id"], second_id)

        token, signals = object(), AsyncSignals()
        self.window._ai_requests[first_id] = (token, signals)
        self.window._async_signals.add(signals)
        self.window._ai_succeeded(token, signals, first_id, "stored description", [1.0])
        self.assertEqual(self.database.get_item(first_id)["ai_description"], "stored description")
        self.assertEqual(self.window.detail.current_item["id"], second_id)

        previous_ids = [item["id"] for item in self.window.current_items]
        old_token, old_signals = object(), AsyncSignals()
        new_token, new_signals = object(), AsyncSignals()
        self.window._semantic_request = (new_token, new_signals)
        self.window._async_signals.update((old_signals, new_signals))
        self.window._semantic_succeeded(old_token, old_signals, "", -1, "", [])
        self.assertEqual([item["id"] for item in self.window.current_items], previous_ids)

    def test_async_task_cancellation_is_tracked_and_waited_for(self):
        token = object()
        started = threading.Event()
        stopped = threading.Event()

        def work(cancel_event):
            started.set()
            cancel_event.wait(2)
            stopped.set()

        self.window._start_async_task(token, work)
        self.assertTrue(started.wait(1))
        self.assertTrue(self.window._cancel_and_wait_for_async_tasks(timeout=1))
        self.assertTrue(stopped.is_set())
        self.assertNotIn(token, self.window._async_tasks)

    def test_startup_library_scan_runs_off_the_ui_thread(self):
        started = threading.Event()
        release = threading.Event()
        worker_threads = []

        def slow_mark_missing(_cancel_event):
            worker_threads.append(threading.current_thread())
            started.set()
            release.wait(2)

        with patch.object(self.database, "mark_missing_files", side_effect=slow_mark_missing), patch.object(
            self.window, "refresh_library"
        ):
            before = time.monotonic()
            self.window._start_startup_scan(False, False)
            self.assertLess(time.monotonic() - before, 0.2)
            self.assertTrue(started.wait(1))
            self.assertIsNot(worker_threads[0], threading.current_thread())
            release.set()
            deadline = time.monotonic() + 2
            while self.window._startup_scan_request is not None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
        self.assertIsNone(self.window._startup_scan_request)

    def test_quit_refuses_to_exit_while_clipboard_write_is_pending(self):
        with patch.object(self.window.clipboard_service.timer, "isActive", return_value=True), patch.object(
            self.window.clipboard_service, "shutdown", return_value=False
        ), patch.object(self.window.clipboard_service, "resume_after_failed_shutdown") as resume, patch(
            "clipsave_app.main_window.QMessageBox.warning"
        ) as warning, patch("clipsave_app.main_window.QApplication.quit") as app_quit, patch.object(
            self.database, "create_backup"
        ) as create_backup, patch.object(self.database, "close") as database_close:
            self.assertFalse(self.window.quit_application())

        resume.assert_called_once_with(True)
        warning.assert_called_once()
        app_quit.assert_not_called()
        create_backup.assert_not_called()
        database_close.assert_not_called()
        self.assertFalse(self.window._closing)
        self.assertFalse(self.window._quit_in_progress)
        self.assertFalse(self.window.force_quit)

    def test_delete_defaults_to_no_and_keeps_item_when_recycle_fails(self):
        text_id = self.window.current_items[0]["id"]
        with patch("clipsave_app.main_window.QMessageBox.question", return_value=QMessageBox.StandardButton.No) as question:
            self.window.delete_item(text_id)
        self.assertEqual(question.call_args.args[4], QMessageBox.StandardButton.No)
        self.assertIsNotNone(self.database.get_item(text_id))

        image_path = Path(self.temp.name) / "managed.png"
        image = QImage(32, 32, QImage.Format.Format_RGB32)
        image.fill(QColor("#21a8fb"))
        self.assertTrue(image.save(str(image_path)))
        image_id = self.database.add_image(image_path)
        with patch("clipsave_app.main_window.is_under_local_store", return_value=True), patch(
            "clipsave_app.main_window.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes
        ), patch("clipsave_app.main_window.send2trash", side_effect=RuntimeError("recycle unavailable")), patch(
            "clipsave_app.main_window.QMessageBox.warning"
        ) as warning, patch.object(self.window, "show_status") as show_status:
            self.window.delete_item(image_id)
        self.assertIsNotNone(self.database.get_item(image_id))
        warning.assert_called_once()
        show_status.assert_called_once_with("删除失败：文件未能移入回收站")

        with patch("clipsave_app.main_window.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), patch.object(
            self.window, "show_status"
        ) as show_status:
            self.window.delete_item(text_id)
        self.assertIsNone(self.database.get_item(text_id))
        show_status.assert_called_with("内容已删除")

    def test_file_actions_report_errors_without_escaping_event_handlers(self):
        image_path = Path(self.temp.name) / "missing.png"
        image = QImage(32, 32, QImage.Format.Format_RGB32)
        image.fill(QColor("#21a8fb"))
        self.assertTrue(image.save(str(image_path)))
        image_id = self.database.add_image(image_path)
        image_path.unlink()

        with patch("clipsave_app.main_window.QMessageBox.warning") as warning, patch.object(
            self.window, "show_status"
        ) as show_status, patch("clipsave_app.main_window.os.startfile") as startfile:
            self.window.activate_item(image_id)
            startfile.assert_not_called()
            warning.assert_called_once()
            show_status.assert_called_once_with("打开失败：图片文件不存在")

        with patch("clipsave_app.main_window.QMessageBox.warning") as warning, patch.object(
            self.window, "show_status"
        ) as show_status:
            self.window.copy_item(image_id)
            warning.assert_called_once()
            show_status.assert_called_once_with("复制失败：图片文件不存在")

    def test_import_continues_after_individual_failure_and_reports_summary(self):
        paths = [str(Path(self.temp.name) / "good.md"), str(Path(self.temp.name) / "blocked.md")]
        with patch("clipsave_app.main_window.QFileDialog.getOpenFileNames", return_value=(paths, "")), patch.object(
            self.database, "import_file", side_effect=[True, PermissionError("access denied")]
        ) as import_file, patch.object(self.window, "refresh_library") as refresh_library, patch.object(
            self.window, "show_status"
        ) as show_status, patch("clipsave_app.main_window.QMessageBox.warning") as warning:
            self.window.import_files()
            deadline = time.monotonic() + 2
            while self.window._import_request is not None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)

        self.assertEqual(import_file.call_count, 2)
        refresh_library.assert_called_once()
        show_status.assert_any_call("已导入 1 项，跳过 0 项重复内容，失败 1 项")
        warning.assert_called_once()
        self.assertIn("blocked.md", warning.call_args.args[2])

    def test_database_delete_failure_keeps_index_and_reports_error(self):
        item_id = self.window.current_items[0]["id"]
        with patch("clipsave_app.main_window.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), patch.object(
            self.database, "remove_item", side_effect=RuntimeError("database busy")
        ), patch("clipsave_app.main_window.QMessageBox.warning") as warning, patch.object(
            self.window, "show_status"
        ) as show_status:
            self.window.delete_item(item_id)

        self.assertIsNotNone(self.database.get_item(item_id))
        warning.assert_called_once()
        show_status.assert_called_once_with("删除失败：内容索引未能移除")


if __name__ == "__main__":
    unittest.main()
