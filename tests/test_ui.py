import ctypes
import os
import tempfile
import threading
import time
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QRect, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from clipsave_app.app import create_app_icon
from clipsave_app.database import ImportFileResult, LibraryDatabase
from clipsave_app.main_window import AsyncSignals, MainWindow
from clipsave_app.settings import Settings
from clipsave_app.widgets import DateDialog, DraggableBar, MarkdownDialog, SettingsDialog, Sidebar
from clipsave_app.windows_frame import MINMAXINFO, WM_GETMINMAXINFO, WM_NCACTIVATE


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
        self.database.close()
        self.temp.cleanup()

    def wait_for_delete(self, item_id: int, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while item_id in self.window._delete_requests and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertNotIn(item_id, self.window._delete_requests)

    def test_panels_and_navigation(self):
        self.assertEqual(self.window.windowTitle(), "ClipSave")
        self.assertTrue(self.window.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertEqual(self.window.window_title_bar.height(), 32)
        self.assertEqual(self.window.brand_label.text, "ClipSave")
        self.assertEqual(self.window.brand_label.objectName(), "BrandTitle")
        self.assertEqual(self.window.brand_label.geometry().x(), 10)
        self.assertEqual(self.window.brand_label.geometry().width(), 190)
        self.assertEqual(self.window.brand_label.geometry().height(), 86)
        self.assertEqual(self.window.brand_label.vertical_scale, 0.86)
        self.window.window_title_bar.maximize_button.click()
        self.app.processEvents()
        self.assertTrue(self.window.isMaximized())
        self.window.window_title_bar.maximize_button.click()
        self.app.processEvents()
        self.assertFalse(self.window.isMaximized())
        self.assertFalse(hasattr(self.window, "capture_state"))
        self.assertIs(self.window.copy_toast.parentWidget(), self.window.centralWidget())
        self.assertFalse(self.window.copy_toast.isVisible())
        self.assertFalse(hasattr(self.window.sidebar, "brand_icon"))
        self.assertFalse(hasattr(self.window.sidebar, "brand"))
        self.assertEqual(
            list(self.window.sidebar.nav_buttons),
            ["all", "favorite", "recent", "image", "text", "markdown", "date"],
        )
        self.assertTrue(self.window.detail.testAttribute(Qt.WidgetAttribute.WA_StyledBackground))
        self.assertEqual(self.window.library_header.height(), 44)
        self.assertEqual(self.window.top_bar.height(), Sidebar.BRAND_AREA_HEIGHT)
        self.assertIsInstance(self.window.top_bar, DraggableBar)
        if os.name == "nt":
            self.assertEqual(self.window.resize_handles, {})
            title_target = self.window.centralWidget().childAt(300, 15)
            self.assertIsNotNone(title_target)
            self.assertEqual(title_target.objectName(), "WindowTitleBar")
        else:
            self.assertEqual(len(self.window.resize_handles), 8)
        scroll_bar = self.window.grid.verticalScrollBar()
        self.assertEqual(scroll_bar.objectName(), "AutoHideScrollBar")
        self.assertFalse(bool(scroll_bar.property("active")))
        scroll_bar.setRange(0, 100)
        scroll_bar.setValue(1)
        self.assertTrue(bool(scroll_bar.property("active")))
        self.assertEqual(self.window.sidebar.collapse_button.text().strip(), "收起侧栏")
        self.assertEqual(self.window.minimumWidth(), 800)
        self.assertEqual(
            self.window.window_title_bar.maximize_button.accessibleName(),
            self.window.window_title_bar.maximize_button.toolTip(),
        )
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
        self.assertEqual(dialog.layout().contentsMargins().left(), 1)
        self.assertEqual(dialog.import_button.text(), "导入文件")
        self.assertFalse(dialog.start_with_windows.isChecked())
        self.assertTrue(dialog.follow_system_theme.isChecked())
        self.assertFalse(dialog.dark_theme_switch.isEnabled())
        self.assertFalse(dialog.auto_ocr.isChecked())
        self.assertFalse(dialog.auto_description.isChecked())
        self.assertEqual(dialog.auto_ocr.text(), "图片自动 OCR")
        self.assertEqual(dialog.auto_description.text(), "图片自动生成描述")
        self.assertEqual(dialog.bulk_image_button.text(), "一键 OCR 并生成全部图片描述")
        import_requests = []
        dialog.import_requested.connect(lambda: import_requests.append(True))
        dialog.import_button.click()
        self.assertEqual(import_requests, [True])
        dialog.close()

        date_dialog = DateDialog(self.database.days(), self.window)
        self.assertTrue(date_dialog.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertEqual(date_dialog.objectName(), "FluentDialog")
        self.assertTrue(date_dialog.property("dateDialog"))
        self.assertEqual(date_dialog.layout().contentsMargins().left(), 1)
        date_dialog.close()
        self.assertFalse(self.window.table.showGrid())
        self.assertTrue(self.window.table.alternatingRowColors())

    def test_new_image_schedules_enabled_automatic_ai_tasks(self):
        image_path = Path(self.temp.name) / "automatic.png"
        image = QImage(8, 8, QImage.Format.Format_RGB32)
        image.fill(QColor("white"))
        self.assertTrue(image.save(str(image_path), "PNG"))
        item_id = self.database.add_image(image_path)
        self.assertIsNotNone(item_id)
        self.settings.data.update(
            {
                "ai_base_url": "http://localhost/v1",
                "ai_vision_model": "vision",
                "auto_ocr": True,
                "auto_description": True,
            }
        )
        with patch.object(self.window, "generate_ocr") as generate_ocr, patch.object(
            self.window, "generate_ai_description"
        ) as generate_description:
            self.window._schedule_auto_image_tasks(item_id)

        generate_ocr.assert_called_once_with(item_id, automatic=True)
        generate_description.assert_called_once_with(item_id, automatic=True)

    def test_settings_persist_automatic_image_options(self):
        dialog = SettingsDialog(self.settings, self.window)
        dialog.base_url.setText("http://localhost/v1")
        dialog.vision_model.setText("vision")
        dialog.auto_ocr.setChecked(True)
        dialog.auto_description.setChecked(True)

        dialog.accept()

        self.assertTrue(self.settings.get("auto_ocr"))
        self.assertTrue(self.settings.get("auto_description"))
        self.assertEqual(self.settings.get("ai_base_url"), "http://localhost/v1")
        self.assertEqual(self.settings.get("ai_vision_model"), "vision")

    def test_settings_reject_automatic_image_options_without_vision_config(self):
        dialog = SettingsDialog(self.settings, self.window)
        dialog.auto_ocr.setChecked(True)

        with patch("clipsave_app.widgets.QMessageBox.warning") as warning:
            dialog.accept()

        warning.assert_called_once()
        self.assertFalse(self.settings.get("auto_ocr"))
        self.assertEqual(dialog.result(), 0)

    def test_bulk_image_confirmation_saves_settings_only_after_yes(self):
        image_path = Path(self.temp.name) / "confirm-bulk.png"
        image = QImage(8, 8, QImage.Format.Format_RGB32)
        image.fill(QColor("white"))
        self.assertTrue(image.save(str(image_path), "PNG"))
        self.database.add_image(image_path)
        dialog = SettingsDialog(self.settings, self.window)
        dialog.base_url.setText("http://localhost/v1")
        dialog.vision_model.setText("vision")

        with patch(
            "clipsave_app.main_window.QMessageBox.question",
            return_value=QMessageBox.StandardButton.No,
        ):
            self.window._confirm_bulk_image_processing(dialog)
        self.assertFalse(dialog.bulk_processing_confirmed)
        self.assertEqual(dialog.result(), 0)

        with patch(
            "clipsave_app.main_window.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.window._confirm_bulk_image_processing(dialog)
        self.assertTrue(dialog.bulk_processing_confirmed)
        self.assertEqual(dialog.result(), 1)
        self.assertEqual(self.settings.get("ai_base_url"), "http://localhost/v1")
        dialog.close()

    def test_bulk_image_processing_runs_ocr_then_description_serially(self):
        image_ids = []
        for index, color in enumerate(("red", "blue"), 1):
            path = Path(self.temp.name) / f"bulk-{index}.png"
            image = QImage(8, 8, QImage.Format.Format_RGB32)
            image.fill(QColor(color))
            self.assertTrue(image.save(str(path), "PNG"))
            image_ids.append(self.database.add_image(path))
        self.settings.data.update(
            {
                "ai_base_url": "http://localhost/v1",
                "ai_vision_model": "vision",
                "ai_embedding_model": "",
            }
        )

        with patch.object(self.window, "_start_async_task") as start_task, patch(
            "clipsave_app.main_window.AIService.ocr_image",
            side_effect=["first OCR", "second OCR"],
        ) as ocr, patch(
            "clipsave_app.main_window.AIService.describe_image",
            side_effect=["first description", "second description"],
        ) as describe, patch(
            "clipsave_app.main_window.AIService.embed",
            return_value=None,
        ) as embed:
            self.window.start_bulk_image_processing()
            worker = start_task.call_args.args[1]
            worker(threading.Event())
            self.app.processEvents()

        self.assertEqual(ocr.call_count, 2)
        self.assertEqual(describe.call_count, 2)
        embed.assert_not_called()
        self.assertIsNone(self.window._bulk_image_request)
        for item_id, expected_ocr, expected_description in zip(
            image_ids,
            ("first OCR", "second OCR"),
            ("first description", "second description"),
        ):
            item = self.database.get_item(item_id)
            self.assertEqual(item["ocr_text"], expected_ocr)
            self.assertEqual(item["ai_description"], expected_description)

    def test_bulk_image_processing_continues_after_embedding_endpoint_failure(self):
        image_ids = []
        for index, color in enumerate(("red", "blue"), 1):
            path = Path(self.temp.name) / f"bulk-embedding-{index}.png"
            image = QImage(8, 8, QImage.Format.Format_RGB32)
            image.fill(QColor(color))
            self.assertTrue(image.save(str(path), "PNG"))
            image_ids.append(self.database.add_image(path))
        self.settings.data.update(
            {
                "ai_base_url": "http://localhost/v1",
                "ai_vision_model": "vision",
                "ai_embedding_model": "unsupported-embedding",
            }
        )

        with patch.object(self.window, "_start_async_task") as start_task, patch(
            "clipsave_app.main_window.AIService.ocr_image",
            side_effect=["first OCR", "second OCR"],
        ) as ocr, patch(
            "clipsave_app.main_window.AIService.describe_image",
            side_effect=["first description", "second description"],
        ) as describe, patch(
            "clipsave_app.main_window.AIService.embed",
            side_effect=RuntimeError("embedding endpoint returned 404"),
        ) as embed, patch(
            "clipsave_app.main_window.QMessageBox.warning"
        ) as warning:
            self.window.start_bulk_image_processing()
            worker = start_task.call_args.args[1]
            worker(threading.Event())
            self.app.processEvents()

        self.assertEqual(ocr.call_count, 2)
        self.assertEqual(describe.call_count, 2)
        embed.assert_called_once()
        self.assertIsNone(self.window._bulk_image_request)
        self.assertEqual(warning.call_count, 1)
        self.assertEqual(warning.call_args.args[1], "向量生成已跳过")
        for item_id, expected_description in zip(
            image_ids,
            ("first description", "second description"),
        ):
            item = self.database.get_item(item_id)
            self.assertEqual(item["ai_description"], expected_description)
            self.assertIsNone(item["embedding"])

    def test_manual_description_is_saved_when_embedding_endpoint_fails(self):
        path = Path(self.temp.name) / "manual-embedding-failure.png"
        image = QImage(8, 8, QImage.Format.Format_RGB32)
        image.fill(QColor("white"))
        self.assertTrue(image.save(str(path), "PNG"))
        item_id = self.database.add_image(path)
        self.settings.data.update(
            {
                "ai_base_url": "http://localhost/v1",
                "ai_vision_model": "vision",
                "ai_embedding_model": "unsupported-embedding",
            }
        )

        with patch.object(self.window, "_start_bounded_task") as start_task, patch(
            "clipsave_app.main_window.AIService.describe_image",
            return_value="saved description",
        ), patch(
            "clipsave_app.main_window.AIService.embed",
            side_effect=RuntimeError("embedding endpoint returned 404"),
        ), patch.object(self.window, "show_error_status") as status:
            self.assertTrue(self.window.generate_ai_description(item_id))
            worker = start_task.call_args.args[1]
            worker(threading.Event())
            self.app.processEvents()

        item = self.database.get_item(item_id)
        self.assertEqual(item["ai_description"], "saved description")
        self.assertIsNone(item["embedding"])
        self.assertNotIn(item_id, self.window._ai_requests)
        status.assert_called_once()
        self.assertIn("向量生成失败", status.call_args.args[0])

    def test_detail_splitter_resizes_the_visible_panel(self):
        item_id = self.window.current_items[0]["id"]
        self.window.select_item(item_id)
        self.window.toggle_detail()
        self.app.processEvents()

        self.assertEqual(self.window.content_splitter.handleWidth(), 8)
        self.assertEqual(
            self.window.content_splitter.handle(1).cursor().shape(),
            Qt.CursorShape.SizeHorCursor,
        )
        initial_width = self.window.detail.width()
        target_width = 460
        total = self.window.content_splitter.width()
        self.window.content_splitter.setSizes([max(1, total - target_width), target_width])
        self.app.processEvents()

        self.assertGreater(self.window.detail.width(), initial_width)
        self.assertLessEqual(self.window.detail.width(), self.window.detail.maximumWidth())

    def test_localized_image_import_is_scheduled_for_automatic_processing(self):
        path = Path(self.temp.name) / "localized.png"
        image = QImage(8, 8, QImage.Format.Format_RGB32)
        image.fill(QColor("white"))
        self.assertTrue(image.save(str(path), "PNG"))
        item_id = self.database.add_image(path)
        self.assertIsNotNone(item_id)

        with patch(
            "clipsave_app.main_window.QFileDialog.getOpenFileNames",
            return_value=([str(path)], ""),
        ), patch.object(
            self.database, "import_file", return_value=ImportFileResult.LOCALIZED
        ), patch.object(
            self.database,
            "indexed_file_for_hash",
            return_value={"id": item_id},
        ), patch.object(
            self.window, "_schedule_auto_image_tasks"
        ) as schedule:
            self.window.settings.data.update(
                {
                    "ai_base_url": "http://localhost/v1",
                    "ai_vision_model": "vision",
                    "auto_ocr": True,
                }
            )
            self.window.import_files()
            deadline = time.monotonic() + 2
            while self.window._import_request is not None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)

        schedule.assert_called_once_with(item_id)

    def test_automatic_ai_failure_cleans_request_without_modal(self):
        item_id = self.window.current_items[0]["id"]
        token, signals = object(), AsyncSignals()
        self.window._ai_requests[item_id] = (token, signals)
        self.window._automatic_ai_items.add(item_id)

        with patch.object(self.window, "show_error_status") as status, patch(
            "clipsave_app.main_window.QMessageBox.warning"
        ) as warning:
            self.window._ai_failed(token, signals, item_id, "provider failed")

        self.assertNotIn(item_id, self.window._ai_requests)
        self.assertNotIn(item_id, self.window._automatic_ai_items)
        status.assert_called_once_with("自动生成描述失败：provider failed")
        warning.assert_not_called()

    def test_stale_ai_or_ocr_failure_does_not_touch_a_new_request(self):
        item_id = self.window.current_items[0]["id"]
        old_ai = (object(), AsyncSignals())
        new_ai = (object(), AsyncSignals())
        old_ocr = (object(), AsyncSignals())
        new_ocr = (object(), AsyncSignals())
        self.window._ai_requests[item_id] = new_ai
        self.window._ocr_requests[item_id] = new_ocr
        self.window._automatic_ai_items.add(item_id)
        self.window._automatic_ocr_items.add(item_id)

        with patch.object(self.window, "show_error_status") as status, patch(
            "clipsave_app.main_window.QMessageBox.warning"
        ) as warning:
            self.window._ai_failed(old_ai[0], old_ai[1], item_id, "old ai failure")
            self.window._ocr_failed(old_ocr[0], old_ocr[1], item_id, "old ocr failure")

        self.assertIs(self.window._ai_requests[item_id], new_ai)
        self.assertIs(self.window._ocr_requests[item_id], new_ocr)
        self.assertIn(item_id, self.window._automatic_ai_items)
        self.assertIn(item_id, self.window._automatic_ocr_items)
        status.assert_not_called()
        warning.assert_not_called()

    def test_manual_ocr_uses_the_configured_vision_service(self):
        image_path = Path(self.temp.name) / "manual-ocr.png"
        image = QImage(8, 8, QImage.Format.Format_RGB32)
        image.fill(QColor("white"))
        self.assertTrue(image.save(str(image_path), "PNG"))
        item_id = self.database.add_image(image_path)
        self.settings.data.update(
            {
                "ai_base_url": "http://localhost/v1",
                "ai_vision_model": "vision",
            }
        )

        with patch(
            "clipsave_app.main_window.AIService.ocr_image",
            return_value="visible text",
        ) as ocr, patch.object(self.window, "_start_bounded_task") as start:
            self.assertTrue(self.window.generate_ocr(item_id))
            work = start.call_args.args[1]
            work(threading.Event())
            self.app.processEvents()

        ocr.assert_called_once()
        self.assertEqual(self.database.get_item(item_id)["ocr_text"], "visible text")

    def test_window_geometry_is_kept_inside_the_available_screen(self):
        screen = Mock()
        screen.availableGeometry.return_value = QRect(0, 0, 1000, 700)
        self.window.setGeometry(900, 650, 500, 500)
        with patch.object(self.window, "screen", return_value=screen):
            self.window._constrain_to_available_screen()
        self.assertEqual(self.window.geometry(), QRect(200, 200, 800, 500))

    def test_interactive_resize_defers_grid_layout_until_finished(self):
        with patch.object(self.window.grid, "set_layout_updates_suspended") as suspended:
            self.window._begin_interactive_resize()
            self.window._begin_interactive_resize()
            self.window._end_interactive_resize()
            self.window._end_interactive_resize()

        self.assertEqual([call.args for call in suspended.call_args_list], [(True,), (False,)])

    def test_windows_native_event_handles_ncactivate_without_affecting_other_messages(self):
        message = wintypes.MSG()
        message.hWnd = int(self.window.winId())
        message.message = WM_NCACTIVATE
        message.wParam = 0

        with patch("clipsave_app.main_window.handle_ncactivate", return_value=(True, 7)) as handler:
            result = self.window.nativeEvent(
                b"windows_generic_MSG", ctypes.addressof(message)
            )

        self.assertEqual(result, (True, 7))
        handler.assert_called_once_with(message.hWnd, 0)

    def test_windows_native_event_constrains_native_maximize_to_work_area(self):
        message = wintypes.MSG()
        message.hWnd = int(self.window.winId())
        message.message = WM_GETMINMAXINFO
        message.wParam = 0
        minimum = MINMAXINFO()
        message.lParam = ctypes.addressof(minimum)

        with patch(
            "clipsave_app.main_window.window_dpi_scale", return_value=1.5
        ), patch(
            "clipsave_app.main_window.handle_getminmaxinfo", return_value=(True, 0)
        ) as handler:
            result = self.window.nativeEvent(
                b"windows_generic_MSG", ctypes.addressof(message)
            )

        self.assertEqual(result, (True, 0))
        handler.assert_called_once_with(
            message.hWnd,
            0,
            message.lParam,
            (1200, 660),
        )

    def test_toggle_maximized_uses_native_restore_for_aero_snap(self):
        with patch(
            "clipsave_app.main_window.is_windows_qt_platform", return_value=True
        ), patch(
            "clipsave_app.main_window.native_window_is_maximized", return_value=True
        ), patch(
            "clipsave_app.main_window.restore_native_window", return_value=True
        ) as restore, patch.object(self.window, "showNormal") as show_normal:
            self.window.toggle_maximized()

        restore.assert_called_once_with(int(self.window.winId()))
        show_normal.assert_not_called()

    def test_toggle_maximized_enters_native_windows_maximize_state(self):
        with patch(
            "clipsave_app.main_window.is_windows_qt_platform", return_value=True
        ), patch(
            "clipsave_app.main_window.native_window_is_maximized", return_value=False
        ), patch(
            "clipsave_app.main_window.maximize_native_window", return_value=True
        ) as maximize, patch.object(self.window, "showMaximized") as show_maximized:
            self.window.toggle_maximized()

        maximize.assert_called_once_with(int(self.window.winId()))
        show_maximized.assert_not_called()

    def test_native_acrylic_retries_once_when_hidden_window_application_fails(self):
        with patch("clipsave_app.main_window.is_windows_qt_platform", return_value=True), patch.object(
            self.window, "winId", return_value=123
        ), patch(
            "clipsave_app.main_window.apply_windows_acrylic", side_effect=[False, True]
        ) as acrylic:
            self.window._native_acrylic_hwnd = None
            self.window._apply_native_acrylic()
            self.window._apply_native_acrylic()

        self.assertEqual(acrylic.call_count, 2)
        self.assertEqual(self.window._native_acrylic_hwnd, 123)

    def test_move_resize_and_detail_toggle_do_not_force_window_back_on_screen(self):
        with patch.object(self.window, "_constrain_to_available_screen") as constrain:
            self.window._begin_interactive_resize()
            self.window._end_interactive_resize()
            self.window.toggle_detail()

        constrain.assert_not_called()

    def test_sidebar_animation_suspends_grid_and_table_repaints(self):
        with patch.object(self.window.grid, "set_layout_updates_suspended") as grid, patch.object(
            self.window.table, "setUpdatesEnabled"
        ) as table_updates, patch.object(self.window.table.viewport(), "update") as viewport_update:
            self.window._begin_sidebar_animation()
            self.window._end_sidebar_animation()

        self.assertEqual([call.args for call in grid.call_args_list], [(True,), (False,)])
        self.assertEqual(
            [call.args for call in table_updates.call_args_list], [(False,), (True,)]
        )
        viewport_update.assert_called_once_with()

    def test_windows_resize_hit_test_only_uses_narrow_l_shaped_edges(self):
        hit = self.window._windows_resize_hit_test
        bounds = (0, 0, 1000, 700, 1.0)

        self.assertEqual(hit(4, 350, *bounds), 10)
        self.assertEqual(hit(996, 350, *bounds), 11)
        self.assertEqual(hit(500, 4, *bounds), 12)
        self.assertEqual(hit(500, 696, *bounds), 15)
        self.assertEqual(hit(4, 10, *bounds), 13)
        self.assertEqual(hit(10, 4, *bounds), 13)
        self.assertIsNone(hit(10, 10, *bounds))
        self.assertIsNone(hit(40, 660, *bounds))
        self.assertIsNone(hit(-1, 350, *bounds))
        self.assertIsNone(hit(1000, 350, *bounds))
        self.assertIsNone(hit(500, -1, *bounds))
        self.assertIsNone(hit(500, 700, *bounds))

    def test_windows_resize_hit_test_covers_all_corners_and_scaled_monitors(self):
        hit = self.window._windows_resize_hit_test
        cases = (
            ((-1200, 200, -200, 900, 1.0), (-1196, 204), 13),
            ((-1200, 200, -200, 900, 1.0), (-204, 204), 14),
            ((-1200, 200, -200, 900, 1.0), (-1196, 896), 16),
            ((-1200, 200, -200, 900, 1.0), (-204, 896), 17),
            ((100, -800, 1100, -100, 1.25), (105, -450), 10),
            ((100, -800, 1100, -100, 1.5), (1094, -450), 11),
            ((100, -800, 1100, -100, 2.0), (600, -785), 12),
            ((100, -800, 1100, -100, 2.0), (600, -115), 15),
        )
        for bounds, point, expected in cases:
            with self.subTest(bounds=bounds, point=point):
                self.assertEqual(hit(*point, *bounds), expected)

        scaled_bounds = (100, 100, 1100, 800, 2.0)
        self.assertEqual(hit(115, 115, *scaled_bounds), 13)
        self.assertIsNone(hit(117, 117, *scaled_bounds))
        self.assertEqual(hit(115, 128, *scaled_bounds), 10)
        self.assertIsNone(hit(116, 128, *scaled_bounds))

    def test_theme_follows_system_and_can_be_disabled_in_settings(self):
        self.settings.data["follow_system_theme"] = True
        with patch("clipsave_app.main_window.system_uses_dark_theme", return_value=True), patch(
            "clipsave_app.main_window.apply_windows_acrylic"
        ) as acrylic:
            self.window.apply_theme(force=True)
            self.app.processEvents()
        self.assertTrue(self.window.dark_theme)
        self.assertTrue(self.app.property("darkTheme"))
        self.assertIn("#202020", self.window.styleSheet())
        self.assertIn("rgba(32,32,32,204)", self.window.styleSheet())
        self.assertIn(
            "QWidget#ContentSurface { background: transparent; }",
            self.window.styleSheet(),
        )
        self.assertNotIn(
            "QWidget#ContentSurface { background: #202020; }",
            self.window.styleSheet(),
        )
        self.assertIn(
            "QScrollBar#AutoHideScrollBar::add-page:vertical, "
            "QScrollBar#AutoHideScrollBar::sub-page:vertical { background: #202020; }",
            self.window.styleSheet(),
        )
        self.assertIn(
            "QFrame#CopyToast { background: rgba(40,40,40,230);",
            self.window.styleSheet(),
        )
        self.assertNotIn("rgba(0,0,0,204)", self.window.styleSheet())
        acrylic.assert_called_with(self.window, True)

        self.settings.data["follow_system_theme"] = False
        self.settings.data["theme_mode"] = "light"
        self.window.apply_theme(force=True)
        self.assertFalse(self.window.dark_theme)
        self.assertFalse(self.app.property("darkTheme"))
        self.assertNotIn(
            "QWidget#ContentSurface { background: #202020; }",
            self.window.styleSheet(),
        )
        self.assertIn(
            "QScrollBar#AutoHideScrollBar::add-page:vertical, "
            "QScrollBar#AutoHideScrollBar::sub-page:vertical { background: #f6f6f6; }",
            self.window.styleSheet(),
        )
        self.assertIn(
            "QWidget#DialogContent { background: #f6f6f6; }",
            self.window.styleSheet(),
        )
        self.assertIn(
            "QFrame#CopyToast { background: rgba(255,255,255,230);",
            self.window.styleSheet(),
        )
        self.assertIn(
            "QListWidget#DateList::item { padding: 0 12px; border-radius: 4px; "
            "background: #ffffff; }",
            self.window.styleSheet(),
        )

        self.settings.data["theme_mode"] = "dark"
        self.window.apply_theme(force=True)
        self.assertTrue(self.window.dark_theme)
        self.settings.data["theme_mode"] = "light"
        self.window.apply_theme(force=True)

    def test_saved_sort_mode_initializes_sort_button_label(self):
        self.window.close()
        self.database.close()
        root = Path(self.temp.name)
        self.database = LibraryDatabase(root / "sorted.db")
        self.database.add_text("sorted")
        self.settings.set("sort", "name")
        self.window = MainWindow(
            self.database, self.settings, create_app_icon(), scan_on_start=False
        )

        self.assertIn("名称", self.window.sort_button.text())

    def test_compact_toolbar_controls_do_not_overlap(self):
        self.window.sidebar.set_collapsed(False, animate=False)
        self.window.resize(800, 520)
        self.app.processEvents()
        controls = [
            self.window.search,
            self.window.semantic_button,
            self.window.sort_button,
            self.window.grid_button,
            self.window.list_button,
            self.window.detail_button,
            self.window.capture_status,
        ]
        geometries = [control.geometry() for control in controls]
        for left, right in zip(geometries, geometries[1:]):
            self.assertLessEqual(left.right(), right.left())

    def test_library_pagination_loads_every_item_once_and_preserves_selection(self):
        for index in range(4):
            self.database.add_text(f"paged item {index}")
        self.window.ITEM_PAGE_SIZE = 2
        self.window.current_sort = "oldest"
        self.window.refresh_items()

        self.assertEqual(len(self.window.current_items), 2)
        self.assertTrue(self.window._items_has_more)
        self.assertEqual(self.window._items_offset, 2)
        self.assertEqual(self.window.result_count.text(), "2+ 项")
        selected_id = self.window.current_items[0]["id"]
        self.window.select_item(selected_id)

        self.window.load_more_items()
        self.assertEqual(len(self.window.current_items), 4)
        self.assertEqual(self.window.current_item_id, selected_id)
        self.assertEqual(self.window._items_offset, 4)
        self.assertTrue(self.window._items_has_more)

        self.window.load_more_items()
        self.assertEqual(len(self.window.current_items), 5)
        self.assertEqual(self.window.current_item_id, selected_id)
        self.assertEqual(self.window._items_offset, 5)
        self.assertFalse(self.window._items_has_more)
        self.assertEqual(self.window.result_count.text(), "5 项")
        ids = [item["id"] for item in self.window.current_items]
        self.assertEqual(len(ids), len(set(ids)))

    def test_library_pagination_only_loads_inside_scroll_threshold(self):
        view = Mock()
        scroll_bar = Mock()
        scroll_bar.maximum.return_value = 500
        view.verticalScrollBar.return_value = scroll_bar

        with patch.object(self.window, "load_more_items") as load_more:
            self.window._load_more_items_if_needed(view, 379)
            load_more.assert_not_called()
            self.window._load_more_items_if_needed(view, 380)
            load_more.assert_called_once_with()

    def test_new_filter_resets_pagination_offset_and_results(self):
        for index in range(4):
            self.database.add_text(f"needle page {index}")
        self.window.ITEM_PAGE_SIZE = 2
        self.window.refresh_items()
        self.window.load_more_items()
        self.assertEqual(self.window._items_offset, 4)

        self.window.search.setText("needle")
        self.window.refresh_items()

        self.assertEqual(self.window._items_offset, 2)
        self.assertEqual(len(self.window.current_items), 2)
        self.assertTrue(self.window._items_has_more)
        self.assertTrue(all("needle" in item["content"] for item in self.window.current_items))

    def test_text_copy_invalidates_an_older_image_copy_completion(self):
        text_id = self.window.current_items[0]["id"]
        token = object()
        signals = AsyncSignals()
        image_id = 999
        self.window._copy_request = (token, signals, image_id)
        image = QImage(8, 8, QImage.Format.Format_RGB32)
        image.fill(QColor("#21a8fb"))

        with patch.object(self.window, "_cancel_async_token") as cancel, patch.object(
            QApplication.clipboard(), "setText"
        ) as set_text, patch.object(QApplication.clipboard(), "setImage") as set_image, patch.object(
            self.window.copy_toast, "show_confirmation"
        ) as show_confirmation:
            self.window.copy_item(text_id)
            self.window._copy_image_succeeded(token, signals, image_id, image)

        cancel.assert_called_once_with(token)
        set_text.assert_called_once_with("first clipboard item")
        set_image.assert_not_called()
        show_confirmation.assert_called_once_with()
        self.assertIsNone(self.window._copy_request)

    def test_capture_refresh_preserves_semantic_results_and_error_tooltip(self):
        self.window._semantic_results_active = True
        with patch.object(self.window, "_refresh_navigation_metadata") as refresh_metadata, patch.object(
            self.window, "refresh_library"
        ) as refresh_library:
            self.window.on_captured(1)
        refresh_metadata.assert_called_once_with()
        refresh_library.assert_not_called()

        old_generation = self.window._status_generation
        self.window.show_error_status("backup unavailable")
        error_tooltip = self.window.capture_status.toolTip()
        self.window._restore_capture_tooltip(old_generation)
        self.assertEqual(self.window.capture_status.toolTip(), error_tooltip)
        self.assertIn("backup unavailable", error_tooltip)

    def test_copy_shortcut_preserves_selected_detail_text(self):
        self.window.detail.ocr_text.setText("selected OCR text")
        self.window.detail.ocr_text.setSelection(0, 8)
        self.window.detail.ocr_text.setFocus()
        with patch.object(self.window, "copy_item") as copy_item, patch.object(
            QApplication.clipboard(), "setText"
        ) as set_text:
            self.window._copy_focused_selection_or_item()

        set_text.assert_called_once_with("selected")
        copy_item.assert_not_called()

    def test_delete_shortcut_edits_search_instead_of_deleting_item(self):
        self.window.search.setText("abcd")
        self.window.search.setCursorPosition(1)
        self.window.search.setFocus()
        with patch.object(self.window, "delete_item") as delete_item:
            self.window._delete_focused_text_or_item()

        self.assertEqual(self.window.search.text(), "acd")
        delete_item.assert_not_called()

    def test_error_tooltip_schedules_restore(self):
        with patch("clipsave_app.main_window.QTimer.singleShot") as single_shot:
            self.window.show_error_status("temporary failure")

        self.assertEqual(single_shot.call_args.args[0], 5000)

    def test_monitor_toggle_shows_transient_status_notification(self):
        with patch.object(self.window.clipboard_service, "start"), patch.object(
            self.window.clipboard_service.timer, "isActive", side_effect=[False, True]
        ), patch.object(self.window, "_show_monitor_notification") as notify:
            self.window.toggle_monitor()

        notify.assert_called_once_with(True)

        with patch("clipsave_app.main_window.QToolTip.showText") as show_tooltip:
            self.window._show_monitor_notification(False)

        show_tooltip.assert_called_once()
        self.assertEqual(show_tooltip.call_args.args[1], "本地自动捕获已暂停")
        self.assertIs(show_tooltip.call_args.args[2], self.window.capture_status)
        self.assertTrue(show_tooltip.call_args.args[3].isNull())

    def test_monitor_toggle_does_not_change_runtime_when_setting_save_fails(self):
        with patch.object(
            self.window.settings, "set", side_effect=OSError("disk full")
        ), patch.object(self.window.clipboard_service, "start") as start, patch.object(
            self.window.clipboard_service, "stop"
        ) as stop, patch.object(self.window, "show_error_status") as show_error, patch.object(
            self.window, "_show_monitor_notification"
        ) as notify:
            self.window.toggle_monitor()

        start.assert_not_called()
        stop.assert_not_called()
        notify.assert_not_called()
        show_error.assert_called_once()

    def test_empty_semantic_search_uses_fluent_information_dialog(self):
        with patch("clipsave_app.main_window.QMessageBox.information") as information:
            self.window.semantic_search()

        information.assert_called_once_with(
            self.window,
            "语义搜索",
            "先输入要查找的画面或含义。",
        )

    def test_favorite_mutation_preserves_semantic_result_order(self):
        item_id = self.window.current_items[0]["id"]
        self.window.search.setText("query that is not in the item")
        self.window.search_timer.stop()
        self.window._semantic_ordered_ids = [item_id]
        self.window._semantic_results_active = True

        self.window.set_favorite(item_id, True)

        self.assertTrue(self.window._semantic_results_active)
        self.assertEqual([item["id"] for item in self.window.current_items], [item_id])

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

    def test_library_refresh_uses_summary_rows_and_view_switch_syncs_selection(self):
        second_id = self.database.add_text("second item")
        with patch.object(self.database, "query_items", wraps=self.database.query_items) as query_items:
            self.window.refresh_items()
        self.assertTrue(query_items.call_args.kwargs["summary_only"])

        first_id = next(item["id"] for item in self.window.current_items if item["id"] != second_id)
        self.window.set_view_mode("grid")
        self.window.select_item(first_id)
        self.window.set_view_mode("list")
        self.window.select_item(second_id)
        self.window.set_view_mode("grid")

        self.assertEqual(self.window.current_item_id, second_id)
        self.assertEqual(self.window.grid.currentIndex().data(self.window.grid._asset_model.IdRole), second_id)

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
        semantic_ids = [item["id"] for item in self.window.current_items]
        self.window.search.setText("nonmatching semantic query")
        self.window.search_timer.stop()
        self.window._semantic_ordered_ids = semantic_ids
        self.window._semantic_results_active = True
        self.window._ai_succeeded(token, signals, first_id, "stored description", [1.0])
        self.assertEqual(self.database.get_item(first_id)["ai_description"], "stored description")
        self.assertEqual(self.window.detail.current_item["id"], second_id)
        self.assertTrue(self.window._semantic_results_active)
        self.assertEqual([item["id"] for item in self.window.current_items], semantic_ids)

        previous_ids = [item["id"] for item in self.window.current_items]
        old_token, old_signals = object(), AsyncSignals()
        new_token, new_signals = object(), AsyncSignals()
        self.window._semantic_request = (new_token, new_signals)
        self.window._async_signals.update((old_signals, new_signals))
        self.window._semantic_succeeded(old_token, old_signals, "", -1, "", [])
        self.assertEqual([item["id"] for item in self.window.current_items], previous_ids)

    def test_ai_result_is_discarded_when_item_hash_changes(self):
        image_path = Path(self.temp.name) / "changed-during-ai.png"
        image = QImage(16, 16, QImage.Format.Format_RGB32)
        image.fill(QColor("#21a8fb"))
        self.assertTrue(image.save(str(image_path)))
        item_id = self.database.add_image(image_path)
        expected_hash = self.database.get_item(item_id)["content_hash"]
        token, signals = object(), AsyncSignals()
        self.window._ai_requests[item_id] = (token, signals)
        with self.database._transaction():
            self.database.connection.execute(
                "UPDATE items SET content_hash=? WHERE id=?",
                ("f" * 64, item_id),
            )

        self.window._ai_succeeded(
            token,
            signals,
            item_id,
            "stale description",
            [1.0],
            expected_hash,
        )

        self.assertEqual(self.database.get_item(item_id)["ai_description"], "")
        self.assertNotIn(item_id, self.window._ai_requests)

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

    def test_bounded_task_must_finish_before_shutdown_can_succeed(self):
        release = threading.Event()
        started = threading.Event()
        token = object()

        def work(_cancel_event):
            started.set()
            release.wait(2)

        handle = self.window._start_bounded_task(token, work)
        try:
            self.assertTrue(started.wait(1))
            self.assertFalse(
                self.window._cancel_and_wait_for_async_tasks(timeout=0.05)
            )
            self.assertFalse(handle.done_event.is_set())
        finally:
            release.set()
        self.assertTrue(self.window._cancel_and_wait_for_async_tasks(timeout=1))

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
            self.window.clipboard_service, "wait_for_idle", return_value=False
        ), patch.object(self.window.clipboard_service, "resume_after_failed_shutdown") as resume, patch(
            "clipsave_app.main_window.QMessageBox.warning"
        ) as warning, patch("clipsave_app.main_window.QApplication.exit") as app_quit, patch.object(
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

    def test_quit_refuses_to_exit_when_final_backup_fails(self):
        with patch.object(self.window.clipboard_service.timer, "isActive", return_value=True), patch.object(
            self.window.clipboard_service, "wait_for_idle", return_value=True
        ), patch.object(self.window.clipboard_service, "shutdown") as shutdown, patch.object(
            self.window.clipboard_service, "resume_after_failed_shutdown"
        ) as resume, patch.object(
            self.database, "create_backup", side_effect=OSError("disk full")
        ), patch.object(self.database, "close") as database_close, patch(
            "clipsave_app.main_window.QMessageBox.warning"
        ) as warning, patch("clipsave_app.main_window.QApplication.exit") as app_quit:
            self.assertFalse(self.window.quit_application())

        shutdown.assert_not_called()
        resume.assert_called_once_with(True)
        database_close.assert_not_called()
        app_quit.assert_not_called()
        warning.assert_called_once()
        self.assertFalse(self.window._closing)
        self.assertFalse(self.window._quit_in_progress)
        self.assertFalse(self.window.force_quit)

    def test_failed_clipboard_shutdown_keeps_thumbnail_loaders_usable(self):
        with patch.object(self.window.clipboard_service.timer, "isActive", return_value=True), patch.object(
            self.window.clipboard_service, "wait_for_idle", return_value=True
        ), patch.object(self.database, "create_backup"), patch.object(
            self.window.grid, "wait_for_thumbnail_idle", return_value=True
        ), patch.object(
            self.window.detail, "wait_for_thumbnail_idle", return_value=True
        ), patch.object(
            self.window.clipboard_service, "shutdown", return_value=False
        ), patch.object(
            self.window.clipboard_service, "resume_after_failed_shutdown"
        ), patch("clipsave_app.main_window.QMessageBox.warning"):
            self.assertFalse(self.window.quit_application())

        self.assertFalse(self.window.grid._thumbnail_loader._closed)
        self.assertFalse(self.window.detail._thumbnail_loader._closed)

    def test_quit_flushes_focused_notes_before_final_backup(self):
        item_id = self.window.current_items[0]["id"]
        self.window.detail.set_item(self.database.get_item(item_id))
        self.window.detail.notes.setPlainText("unsaved focused note")

        with patch.object(self.window.clipboard_service.timer, "isActive", return_value=False), patch.object(
            self.window.clipboard_service, "wait_for_idle", return_value=True
        ), patch.object(
            self.database, "create_backup", side_effect=OSError("stop after note flush")
        ), patch("clipsave_app.main_window.QMessageBox.warning"):
            self.assertFalse(self.window.quit_application())

        self.assertEqual(self.database.get_item(item_id)["notes"], "unsaved focused note")

    def test_failed_note_save_keeps_draft_when_switching_items(self):
        first_id = self.window.current_items[0]["id"]
        second_id = self.database.add_text("second note item")
        self.window.detail.set_item(self.database.get_item(first_id))
        self.window.detail.notes.setPlainText("draft that must survive")

        with patch.object(self.database, "set_notes", side_effect=OSError("disk full")), patch(
            "clipsave_app.main_window.QMessageBox.warning"
        ):
            self.window.detail.set_item(self.database.get_item(second_id))
            self.window.detail.set_item(self.database.get_item(first_id))

        self.assertEqual(self.window.detail.notes.toPlainText(), "draft that must survive")
        self.assertEqual(self.database.get_item(first_id)["notes"], "")

    def test_quit_retries_failed_note_drafts_for_non_current_items(self):
        first_id = self.window.current_items[0]["id"]
        second_id = self.database.add_text("second note item")
        self.window.detail.set_item(self.database.get_item(first_id))
        self.window.detail.notes.setPlainText("draft recovered during quit")
        with patch.object(self.database, "set_notes", side_effect=OSError("disk full")), patch(
            "clipsave_app.main_window.QMessageBox.warning"
        ):
            self.window.detail.set_item(self.database.get_item(second_id))

        with patch.object(
            self.database, "create_backup", side_effect=OSError("stop after draft retry")
        ), patch("clipsave_app.main_window.QMessageBox.warning"):
            self.assertFalse(self.window.quit_application())

        self.assertEqual(
            self.database.get_item(first_id)["notes"], "draft recovered during quit"
        )
        self.assertEqual(self.window.detail.pending_note_drafts(), {})

    def test_removing_active_filter_tag_clears_details(self):
        item_id = self.window.current_items[0]["id"]
        tag_id = self.database.add_tag(item_id, "temporary filter")
        self.window._refresh_navigation_metadata()
        self.window.navigate("tag", tag_id)
        self.window.select_item(item_id)
        self.window.toggle_detail()
        self.assertEqual(self.window.detail.current_item["id"], item_id)

        self.window.remove_tag_from_item(item_id, "temporary filter")

        self.assertEqual(self.window.current_items, [])
        self.assertIsNone(self.window.current_item_id)
        self.assertIsNone(self.window.detail.current_item)

    def test_navigation_active_state_survives_metadata_refresh(self):
        self.window.navigate("favorite", None)
        self.assertTrue(bool(self.window.sidebar.nav_buttons["favorite"].property("active")))
        self.window._refresh_navigation_metadata()
        self.assertTrue(bool(self.window.sidebar.nav_buttons["favorite"].property("active")))

        collection_id = self.database.create_collection("Active collection")
        item_id = self.database.add_text("tagged navigation item")
        tag_id = self.database.add_tag(item_id, "Active tag")
        self.window._refresh_navigation_metadata()
        self.window.navigate("collection", collection_id)
        collection_button = next(
            button
            for button in self.window.sidebar.collection_buttons
            if button.key == f"collection:{collection_id}"
        )
        self.assertTrue(bool(collection_button.property("active")))
        self.window.navigate("tag", tag_id)
        tag_button = next(
            button
            for button in self.window.sidebar.tag_buttons
            if button.key == f"tag:{tag_id}"
        )
        self.assertTrue(bool(tag_button.property("active")))

    def test_collection_delete_defaults_to_no_and_preserves_content(self):
        item_id = self.window.current_items[0]["id"]
        collection_id = self.database.create_collection("Keep for now")
        self.database.set_collection(item_id, collection_id)
        self.window._refresh_navigation_metadata()
        delete_button = self.window.sidebar.collection_delete_buttons[collection_id]

        with patch(
            "clipsave_app.main_window.QMessageBox.question",
            return_value=QMessageBox.StandardButton.No,
        ) as question:
            delete_button.click()

        self.assertEqual(question.call_args.args[4], QMessageBox.StandardButton.No)
        self.assertIn(collection_id, {row["id"] for row in self.database.collections()})
        self.assertEqual(self.database.get_item(item_id)["collection_id"], collection_id)

    def test_deleting_active_collection_returns_to_all_and_keeps_item(self):
        item_id = self.window.current_items[0]["id"]
        collection_id = self.database.create_collection("Delete collection")
        self.database.set_collection(item_id, collection_id)
        self.window._refresh_navigation_metadata()
        self.window.navigate("collection", collection_id)
        self.window.select_item(item_id)
        self.window.toggle_detail()
        delete_button = self.window.sidebar.collection_delete_buttons[collection_id]

        with patch(
            "clipsave_app.main_window.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ), patch.object(self.window, "show_status") as show_status:
            delete_button.click()

        item = self.database.get_item(item_id)
        self.assertIsNotNone(item)
        self.assertIsNone(item["collection_id"])
        self.assertIsNone(self.window.current_collection)
        self.assertEqual(self.window.sidebar.active_key, "all")
        self.assertNotIn(collection_id, self.window.sidebar.collection_delete_buttons)
        self.assertEqual(self.window.detail.current_item["collection_id"], None)
        show_status.assert_called_with("集合已删除")

    def test_deleting_active_tag_returns_to_all_and_keeps_item(self):
        item_id = self.window.current_items[0]["id"]
        tag_id = self.database.add_tag(item_id, "Delete tag")
        self.window._refresh_navigation_metadata()
        self.window.navigate("tag", tag_id)
        self.window.select_item(item_id)
        self.window.toggle_detail()
        delete_button = self.window.sidebar.tag_delete_buttons[tag_id]

        with patch(
            "clipsave_app.main_window.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ), patch.object(self.window, "show_status") as show_status:
            delete_button.click()

        item = self.database.get_item(item_id)
        self.assertIsNotNone(item)
        self.assertFalse(item["tag_names"])
        self.assertIsNone(self.window.current_tag)
        self.assertEqual(self.window.sidebar.active_key, "all")
        self.assertNotIn(tag_id, self.window.sidebar.tag_delete_buttons)
        self.assertFalse(self.window.detail.current_item["tag_names"])
        show_status.assert_called_with("标签已删除")

    def test_failed_final_backup_restores_ai_controls(self):
        image_path = Path(self.temp.name) / "busy.png"
        image = QImage(16, 16, QImage.Format.Format_RGB32)
        image.fill(QColor("#21a8fb"))
        self.assertTrue(image.save(str(image_path)))
        item_id = self.database.add_image(image_path)
        self.window.refresh_items()
        self.window.select_item(item_id)
        self.window.toggle_detail()
        self.window.detail.set_ai_busy(True)
        self.assertFalse(self.window.detail.ai_button.isEnabled())

        with patch.object(
            self.database, "create_backup", side_effect=OSError("disk full")
        ), patch("clipsave_app.main_window.QMessageBox.warning"):
            self.assertFalse(self.window.quit_application())

        self.assertTrue(self.window.detail.ai_button.isEnabled())
        self.assertNotIn("生成中", self.window.detail.ai_button.text())

    def test_failed_collection_change_reloads_saved_detail_value(self):
        item_id = self.window.current_items[0]["id"]
        collection_id = self.database.create_collection("Unsaved collection")
        self.window._refresh_navigation_metadata()
        self.window.select_item(item_id)
        self.window.toggle_detail()
        self.window.detail.collection_combo.blockSignals(True)
        self.window.detail.collection_combo.setCurrentIndex(
            self.window.detail.collection_combo.findData(collection_id)
        )
        self.window.detail.collection_combo.blockSignals(False)

        with patch.object(
            self.database, "set_collection", side_effect=OSError("disk full")
        ), patch("clipsave_app.main_window.QMessageBox.warning"):
            self.window.set_item_collection(item_id, collection_id)

        self.assertIsNone(self.database.get_item(item_id)["collection_id"])
        self.assertIsNone(self.window.detail.collection_combo.currentData())

    def test_settings_reports_global_hotkey_registration_failure(self):
        self.window.global_hotkey_registered = False
        dialog = SettingsDialog(self.settings, self.window)

        self.assertIn("注册失败", dialog.hotkey_status.text())
        dialog.close()

    def test_settings_applies_start_with_windows_change(self):
        def enable_startup(dialog):
            dialog.start_with_windows.setChecked(True)
            dialog.accept()
            return dialog.result()

        with patch.object(
            self.window, "_exec_transient_dialog", side_effect=enable_startup
        ), patch("clipsave_app.main_window.set_start_with_windows") as set_startup:
            self.window.open_settings()

        self.assertTrue(self.settings.get("start_with_windows"))
        set_startup.assert_called_once_with(True)

    def test_start_with_windows_failure_restores_setting(self):
        def enable_startup(dialog):
            dialog.start_with_windows.setChecked(True)
            dialog.accept()
            return dialog.result()

        with patch.object(
            self.window, "_exec_transient_dialog", side_effect=enable_startup
        ), patch(
            "clipsave_app.main_window.set_start_with_windows",
            side_effect=OSError("access denied"),
        ), patch("clipsave_app.main_window.QMessageBox.warning") as warning:
            self.window.open_settings()

        self.assertFalse(self.settings.get("start_with_windows"))
        warning.assert_called_once()

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
        ), patch(
            "clipsave_app.main_window.recycle_managed_file",
            side_effect=RuntimeError("recycle unavailable"),
        ), patch(
            "clipsave_app.main_window.QMessageBox.warning"
        ) as warning, patch.object(self.window, "show_status") as show_status:
            self.window.delete_item(image_id)
            self.wait_for_delete(image_id)
        self.assertIsNotNone(self.database.get_item(image_id))
        warning.assert_called_once()
        show_status.assert_any_call("删除失败：文件未能移入回收站")

        with patch("clipsave_app.main_window.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), patch.object(
            self.window, "show_status"
        ) as show_status:
            self.window.delete_item(text_id)
            self.wait_for_delete(text_id)
        self.assertIsNone(self.database.get_item(text_id))
        show_status.assert_called_with("内容已删除")

    def test_delete_recycle_runs_in_background_and_hides_item_while_pending(self):
        image_path = Path(self.temp.name) / "slow-delete.png"
        image = QImage(32, 32, QImage.Format.Format_RGB32)
        image.fill(QColor("#21a8fb"))
        self.assertTrue(image.save(str(image_path)))
        image_id = self.database.add_image(image_path)
        started = threading.Event()
        release = threading.Event()

        def slow_recycle(*_args, **_kwargs):
            started.set()
            release.wait(2)

        try:
            with patch("clipsave_app.main_window.is_under_local_store", return_value=True), patch(
                "clipsave_app.main_window.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ), patch("clipsave_app.main_window.recycle_managed_file", side_effect=slow_recycle):
                started_at = time.monotonic()
                self.window.delete_item(image_id)
                elapsed = time.monotonic() - started_at
                self.assertLess(elapsed, 0.2)
                self.assertTrue(started.wait(1))
                self.assertIn(image_id, self.window._delete_requests)
                self.assertNotIn(image_id, {item["id"] for item in self.window.current_items})
                release.set()
                self.wait_for_delete(image_id)
        finally:
            release.set()

        self.assertIsNone(self.database.get_item(image_id))

    def test_delete_failure_restores_selected_item(self):
        image_path = Path(self.temp.name) / "restore-delete.png"
        image = QImage(16, 16, QImage.Format.Format_RGB32)
        image.fill(QColor("#21a8fb"))
        self.assertTrue(image.save(str(image_path)))
        image_id = self.database.add_image(image_path)
        self.window.refresh_library()
        self.window.select_item(image_id)

        with patch("clipsave_app.main_window.is_under_local_store", return_value=True), patch(
            "clipsave_app.main_window.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ), patch(
            "clipsave_app.main_window.recycle_managed_file",
            side_effect=RuntimeError("file is locked"),
        ), patch("clipsave_app.main_window.QMessageBox.warning"):
            self.window.delete_item(image_id)
            self.wait_for_delete(image_id)

        self.assertEqual(self.window.current_item_id, image_id)
        self.assertIn(image_id, {item["id"] for item in self.window.current_items})
        self.assertIsNotNone(self.database.get_item(image_id))

    def test_delete_marks_item_missing_when_index_removal_fails_after_recycle(self):
        image_path = Path(self.temp.name) / "managed-delete.png"
        image = QImage(16, 16, QImage.Format.Format_RGB32)
        image.fill(QColor("#21a8fb"))
        self.assertTrue(image.save(str(image_path)))
        image_id = self.database.add_image(image_path)
        ai_request = (object(), AsyncSignals())
        ocr_request = (object(), AsyncSignals())
        self.window._ai_requests[image_id] = ai_request
        self.window._ocr_requests[image_id] = ocr_request

        with patch("clipsave_app.main_window.is_under_local_store", return_value=True), patch(
            "clipsave_app.main_window.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ), patch("clipsave_app.main_window.recycle_managed_file"), patch.object(
            self.database, "remove_item", side_effect=OSError("database busy")
        ), patch("clipsave_app.main_window.QMessageBox.warning"):
            self.window.delete_item(image_id)
            self.wait_for_delete(image_id)

        row = self.database.connection.execute(
            "SELECT * FROM items WHERE id=?", (image_id,)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["missing"], 1)
        self.assertNotIn(image_id, {item["id"] for item in self.database.query_items()})
        self.assertNotIn(image_id, self.window._ai_requests)
        self.assertNotIn(image_id, self.window._ocr_requests)

    def test_delete_hides_item_in_session_when_both_index_updates_fail(self):
        image_path = Path(self.temp.name) / "managed-double-failure.png"
        image = QImage(16, 16, QImage.Format.Format_RGB32)
        image.fill(QColor("#21a8fb"))
        self.assertTrue(image.save(str(image_path)))
        image_id = self.database.add_image(image_path)
        self.window.refresh_library()

        with patch("clipsave_app.main_window.is_under_local_store", return_value=True), patch(
            "clipsave_app.main_window.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ), patch("clipsave_app.main_window.recycle_managed_file"), patch.object(
            self.database, "remove_item", side_effect=OSError("database busy")
        ), patch.object(
            self.database, "mark_item_missing", side_effect=OSError("still busy")
        ), patch("clipsave_app.main_window.QMessageBox.warning"):
            self.window.delete_item(image_id)
            self.wait_for_delete(image_id)

        self.assertIn(image_id, self.window._session_hidden_item_ids)
        self.assertNotIn(image_id, {item["id"] for item in self.window.current_items})
        row = self.database.connection.execute(
            "SELECT missing FROM items WHERE id=?", (image_id,)
        ).fetchone()
        self.assertEqual(row["missing"], 0)

    def test_quit_refuses_to_close_while_compute_task_is_still_active(self):
        item_id = self.window.current_items[0]["id"]
        ai_request = (object(), AsyncSignals())
        ocr_request = (object(), AsyncSignals())
        self.window._ai_requests[item_id] = ai_request
        self.window._ocr_requests[item_id] = ocr_request
        with patch.object(self.window, "_cancel_and_wait_for_async_tasks", return_value=False), patch.object(
            self.window.clipboard_service, "shutdown"
        ) as shutdown, patch("clipsave_app.main_window.QMessageBox.warning") as warning:
            self.assertFalse(self.window.quit_application())

        shutdown.assert_not_called()
        warning.assert_called_once()
        self.assertFalse(self.window._quit_in_progress)
        self.assertFalse(self.window.force_quit)
        self.assertIs(self.window._ai_requests[item_id], ai_request)
        self.assertIs(self.window._ocr_requests[item_id], ocr_request)

    def test_quit_late_failure_restores_completed_semantic_button(self):
        token = object()
        signals = AsyncSignals()
        self.window._semantic_request = (token, signals)
        self.window.semantic_button.setEnabled(False)
        self.window.semantic_button.setText("搜索中…")

        with patch.object(
            self.window, "_cancel_and_wait_for_async_tasks", return_value=True
        ), patch.object(
            self.window.grid, "wait_for_thumbnail_idle", return_value=False
        ), patch("clipsave_app.main_window.QMessageBox.warning"):
            self.assertFalse(self.window.quit_application())

        self.assertIsNone(self.window._semantic_request)
        self.assertTrue(self.window.semantic_button.isEnabled())
        self.assertEqual(self.window.semantic_button.text(), "语义搜索")

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
            self.window.open_item(image_id)
            startfile.assert_not_called()
            warning.assert_called_once()
            show_status.assert_called_once_with("打开失败：图片文件不存在")

        with patch.object(self.window, "copy_item") as copy_item, patch(
            "clipsave_app.main_window.os.startfile"
        ) as startfile:
            self.window.activate_item(image_id)
            copy_item.assert_called_once_with(image_id)
            startfile.assert_not_called()

        with patch("clipsave_app.main_window.QMessageBox.warning") as warning, patch.object(
            self.window, "show_status"
        ) as show_status, patch.object(
            self.window.copy_toast, "show_confirmation"
        ) as show_confirmation:
            self.window.copy_item(image_id)
            warning.assert_called_once()
            show_status.assert_called_once_with("复制失败：图片文件不存在")
            show_confirmation.assert_not_called()

    def test_image_actions_copy_show_details_and_open_in_default_app(self):
        image_path = Path(self.temp.name) / "actions.png"
        image = QImage(32, 32, QImage.Format.Format_RGB32)
        image.fill(QColor("#21a8fb"))
        self.assertTrue(image.save(str(image_path)))
        image_id = self.database.add_image(image_path)
        self.window.refresh_library()

        with patch.object(self.window, "copy_item") as copy_item:
            self.window.activate_item(image_id)
        copy_item.assert_called_once_with(image_id)

        previous_interval = QApplication.doubleClickInterval()
        QApplication.setDoubleClickInterval(20)
        try:
            row = self.window.grid._asset_model.row_for_id(image_id)
            index = self.window.grid.model().index(row, 0)
            point = self.window.grid.delegate.card_rect(
                self.window.grid.visualRect(index)
            ).center()
            self.assertFalse(self.window.detail.isVisible())
            QTest.mouseClick(
                self.window.grid.viewport(), Qt.MouseButton.RightButton, pos=point
            )
            self.app.processEvents()
            self.assertTrue(self.window.detail.isVisible())
            self.assertEqual(self.window.current_item_id, image_id)
            self.assertEqual(self.window.detail.current_item["id"], image_id)
            QTest.qWait(25)
            QTest.mouseClick(
                self.window.grid.viewport(), Qt.MouseButton.RightButton, pos=point
            )
            self.app.processEvents()
            self.assertFalse(self.window.detail.isVisible())
        finally:
            QApplication.setDoubleClickInterval(previous_interval)

        with patch("clipsave_app.main_window.os.startfile") as startfile:
            QTest.qWait(25)
            QTest.mouseClick(
                self.window.grid.viewport(), Qt.MouseButton.LeftButton, pos=point
            )
            QTest.mouseDClick(
                self.window.grid.viewport(), Qt.MouseButton.LeftButton, pos=point
            )
            QTest.mouseClick(
                self.window.grid.viewport(), Qt.MouseButton.LeftButton, pos=point
            )
            self.app.processEvents()
        startfile.assert_called_once()
        opened_path = Path(startfile.call_args.args[0])
        self.assertTrue(opened_path.samefile(image_path))

    def test_image_copy_decodes_in_background_and_database_errors_are_reported(self):
        image_path = Path(self.temp.name) / "copy.png"
        image = QImage(64, 40, QImage.Format.Format_RGB32)
        image.fill(QColor("#21a8fb"))
        self.assertTrue(image.save(str(image_path)))
        image_id = self.database.add_image(image_path)

        with patch.object(self.window.clipboard_service, "suppress_image") as suppress:
            self.window.copy_item(image_id)
            deadline = time.monotonic() + 2
            while self.window._copy_request is not None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
        suppress.assert_called_once()

        with patch.object(self.database, "set_notes", side_effect=RuntimeError("database busy")), patch(
            "clipsave_app.main_window.QMessageBox.warning"
        ) as warning, patch.object(self.window, "show_status") as show_status:
            self.window.save_notes(image_id, "note")
        warning.assert_called_once()
        show_status.assert_called_once_with("备注未保存")

    def test_import_continues_after_individual_failure_and_reports_summary(self):
        paths = [str(Path(self.temp.name) / "good.md"), str(Path(self.temp.name) / "blocked.md")]
        for path in paths:
            Path(path).write_text("content", encoding="utf-8")
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

    def test_invalid_image_import_is_reported_as_failure_not_duplicate(self):
        broken = Path(self.temp.name) / "broken.png"
        broken.write_bytes(b"not an image")

        with patch(
            "clipsave_app.main_window.QFileDialog.getOpenFileNames",
            return_value=([str(broken)], ""),
        ), patch.object(self.window, "show_status") as show_status, patch(
            "clipsave_app.main_window.QMessageBox.warning"
        ) as warning:
            self.window.import_files()
            deadline = time.monotonic() + 2
            while self.window._import_request is not None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)

        show_status.assert_any_call("已导入 0 项，跳过 0 项重复内容，失败 1 项")
        warning.assert_called_once()
        self.assertIn("broken.png", warning.call_args.args[2])

    def test_localized_import_is_reported_separately_from_duplicates(self):
        path = Path(self.temp.name) / "localized.md"
        path.write_text("content", encoding="utf-8")
        with patch(
            "clipsave_app.main_window.QFileDialog.getOpenFileNames",
            return_value=([str(path)], ""),
        ), patch.object(
            self.database, "import_file", return_value=ImportFileResult.LOCALIZED
        ), patch.object(self.window, "show_status") as show_status:
            self.window.import_files()
            deadline = time.monotonic() + 2
            while self.window._import_request is not None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)

        show_status.assert_any_call(
            "已导入 0 项，本地化 1 项，跳过 0 项重复内容，失败 0 项"
        )

    def test_database_delete_failure_keeps_index_and_reports_error(self):
        item_id = self.window.current_items[0]["id"]
        with patch("clipsave_app.main_window.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), patch.object(
            self.database, "remove_item", side_effect=RuntimeError("database busy")
        ), patch("clipsave_app.main_window.QMessageBox.warning") as warning, patch.object(
            self.window, "show_status"
        ) as show_status:
            self.window.delete_item(item_id)
            self.wait_for_delete(item_id)

        self.assertIsNotNone(self.database.get_item(item_id))
        warning.assert_called_once()
        show_status.assert_any_call("删除失败：内容索引未能移除")

    def test_transient_dialogs_are_destroyed_after_exec(self):
        dialogs = [
            DateDialog(self.database.days(), self.window),
            SettingsDialog(self.settings, self.window),
            MarkdownDialog("Example", "# content", None, self.window),
        ]
        for dialog in dialogs:
            with patch.object(dialog, "exec", return_value=0):
                self.window._exec_transient_dialog(dialog)

        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.assertEqual(self.window.findChildren(DateDialog), [])
        self.assertEqual(self.window.findChildren(SettingsDialog), [])
        self.assertEqual(self.window.findChildren(MarkdownDialog), [])

    def test_notes_and_ocr_changes_refresh_active_text_search(self):
        text_id = self.database.add_text("searchable")
        self.database.set_notes(text_id, "needle")
        self.window.search.setText("needle")
        self.window.refresh_items()
        self.assertIn(text_id, {item["id"] for item in self.window.current_items})

        self.assertTrue(self.window.save_notes(text_id, "replacement"))
        self.assertNotIn(text_id, {item["id"] for item in self.window.current_items})

        image_path = Path(self.temp.name) / "ocr-search.png"
        image = QImage(16, 16, QImage.Format.Format_RGB32)
        image.fill(QColor("#21a8fb"))
        self.assertTrue(image.save(str(image_path)))
        image_id = self.database.add_image(image_path)
        self.database.update_ocr(image_id, "needle")
        self.window.refresh_items()
        self.assertIn(image_id, {item["id"] for item in self.window.current_items})

        token = object()
        signals = AsyncSignals()
        self.window._ocr_requests[image_id] = (token, signals)
        self.window._async_signals.add(signals)
        self.window._ocr_succeeded(token, signals, image_id, "replacement", None)
        self.assertNotIn(image_id, {item["id"] for item in self.window.current_items})

    def test_delete_invalidates_pending_item_tasks_and_late_results(self):
        item_id = self.window.current_items[0]["id"]
        ai_request = (object(), AsyncSignals())
        ocr_request = (object(), AsyncSignals())
        self.window._ai_requests[item_id] = ai_request
        self.window._ocr_requests[item_id] = ocr_request
        self.window._async_signals.update((ai_request[1], ocr_request[1]))

        with patch(
            "clipsave_app.main_window.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.window.delete_item(item_id)

        self.assertNotIn(item_id, self.window._ai_requests)
        self.assertNotIn(item_id, self.window._ocr_requests)
        with patch.object(self.database, "update_ocr") as update_ocr, patch.object(
            self.window, "show_status"
        ) as show_status:
            self.window._ocr_succeeded(
                ocr_request[0], ocr_request[1], item_id, "late result", None
            )
        update_ocr.assert_not_called()
        show_status.assert_not_called()

    def test_session_shutdown_failure_is_bounded_noninteractive_and_restores_actions(self):
        with patch.object(
            self.window, "_cancel_and_wait_request", return_value=False
        ), patch("clipsave_app.main_window.QMessageBox.warning") as warning:
            self.assertFalse(self.window.quit_application_for_session_end(0.05))

        warning.assert_not_called()
        self.assertFalse(self.window._quit_in_progress)
        self.assertTrue(self.window.centralWidget().isEnabled())
        self.assertTrue(all(shortcut.isEnabled() for shortcut in self.window.shortcuts))

    def test_successful_session_shutdown_creates_backup_and_closes_database(self):
        application = QApplication.instance()
        with patch.object(
            self.window.clipboard_service, "wait_for_idle", return_value=True
        ), patch.object(
            self.window.clipboard_service, "shutdown", return_value=True
        ), patch.object(
            self.database, "create_backup"
        ) as create_backup, patch.object(
            self.database, "close"
        ) as database_close, patch.object(
            application, "exit"
        ) as exit_app:
            self.assertTrue(self.window.quit_application_for_session_end(1.0))

        create_backup.assert_not_called()
        database_close.assert_called_once_with()
        exit_app.assert_called_once_with(0)

    def test_import_is_rejected_once_session_shutdown_begins(self):
        self.window._quit_in_progress = True
        with patch("clipsave_app.main_window.QFileDialog.getOpenFileNames") as choose_files:
            self.window.import_files()
        choose_files.assert_not_called()

    def test_startup_scan_failure_is_preserved_for_smoke_readiness(self):
        token = object()
        signals = AsyncSignals()
        self.window._startup_scan_request = (token, signals)
        self.window._async_signals.add(signals)

        self.window._startup_scan_failed(token, signals, "scan failed")

        self.assertIsNone(self.window._startup_scan_request)
        self.assertEqual(self.window.startup_scan_error, "scan failed")

    def test_session_shutdown_note_write_respects_deadline(self):
        item_id = self.window.current_items[0]["id"]
        self.window.select_item(item_id)
        self.window.toggle_detail()
        self.window.detail.notes.setPlainText("pending")

        with patch.object(
            self.database,
            "set_notes_if_unchanged",
            side_effect=lambda *_args: time.sleep(0.2),
        ), patch("clipsave_app.main_window.QMessageBox.warning") as warning:
            started = time.monotonic()
            self.assertFalse(self.window.quit_application_for_session_end(0.01))
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.1)
        warning.assert_not_called()
        time.sleep(0.22)

    def test_timed_out_session_note_write_cannot_overwrite_newer_edit(self):
        item_id = self.window.current_items[0]["id"]
        self.window.select_item(item_id)
        self.window.toggle_detail()
        self.window.detail.notes.setPlainText("stale-session-draft")
        started = threading.Event()
        release = threading.Event()
        real_compare_and_set = self.database.set_notes_if_unchanged

        def delayed_compare_and_set(*args):
            started.set()
            release.wait(1)
            return real_compare_and_set(*args)

        with patch.object(
            self.database,
            "set_notes_if_unchanged",
            side_effect=delayed_compare_and_set,
        ):
            self.assertFalse(self.window.quit_application_for_session_end(0.01))
            self.assertTrue(started.is_set())
            self.assertTrue(self.window.save_notes(item_id, "newer-user-edit"))
            release.set()
            time.sleep(0.05)

        self.assertEqual(self.database.get_item(item_id)["notes"], "newer-user-edit")

    def test_note_flush_can_remove_current_search_result_without_readding_draft(self):
        item_id = self.window.current_items[0]["id"]
        self.database.set_notes(item_id, "needle")
        self.window.search.setText("needle")
        self.window.refresh_items()
        self.window.select_item(item_id)
        self.window.toggle_detail()
        self.window.detail.notes.setPlainText("replacement")

        self.assertTrue(self.window.detail.flush_notes())
        self.assertIsNone(self.window.current_item_id)
        self.assertNotIn(item_id, self.window.detail.pending_note_drafts())

    def test_reverting_notes_to_loaded_value_clears_failed_draft(self):
        item_id = self.window.current_items[0]["id"]
        self.database.set_notes(item_id, "original")
        self.window.select_item(item_id)
        self.window.toggle_detail()
        self.window.detail._note_drafts[item_id] = "failed-draft"
        self.window.detail.notes.setPlainText("original")

        self.assertTrue(self.window.detail.flush_notes())
        self.assertNotIn(item_id, self.window.detail.pending_note_drafts())

    def test_ai_completion_cannot_repopulate_detail_removed_by_note_search(self):
        image_path = Path(self.temp.name) / "ai-note-search.png"
        image = QImage(16, 16, QImage.Format.Format_RGB32)
        image.fill(QColor("#21a8fb"))
        self.assertTrue(image.save(str(image_path)))
        item_id = self.database.add_image(image_path)
        self.database.set_notes(item_id, "needle")
        self.window.search.setText("needle")
        self.window.refresh_items()
        self.window.select_item(item_id)
        self.window.toggle_detail()
        self.window.detail.notes.setPlainText("replacement")
        token = object()
        signals = AsyncSignals()
        self.window._ai_requests[item_id] = (token, signals)

        self.window._ai_succeeded(token, signals, item_id, "description", [0.1])

        self.assertIsNone(self.window.current_item_id)
        self.assertIsNone(self.window.detail.current_item)

    def test_reentrant_detail_refresh_uses_newly_saved_note_record(self):
        image_path = Path(self.temp.name) / "ai-note-refresh.png"
        image = QImage(16, 16, QImage.Format.Format_RGB32)
        image.fill(QColor("#21a8fb"))
        self.assertTrue(image.save(str(image_path)))
        item_id = self.database.add_image(image_path)
        self.database.set_notes(item_id, "old note")
        self.window.search.setText("note")
        self.window.refresh_items()
        self.window.select_item(item_id)
        self.window.toggle_detail()
        self.window.detail.notes.setPlainText("new note")
        token = object()
        signals = AsyncSignals()
        self.window._ai_requests[item_id] = (token, signals)

        self.window._ai_succeeded(token, signals, item_id, "description", [0.1])

        self.assertEqual(self.database.get_item(item_id)["notes"], "new note")
        self.assertEqual(self.window.detail.notes.toPlainText(), "new note")
        self.assertEqual(self.window.detail._loaded_notes, "new note")

    def test_ai_ocr_failures_do_not_show_modals_during_session_shutdown(self):
        item_id = self.window.current_items[0]["id"]
        ai_request = (object(), AsyncSignals())
        ocr_request = (object(), AsyncSignals())
        self.window._ai_requests[item_id] = ai_request
        self.window._ocr_requests[item_id] = ocr_request
        self.window._quit_in_progress = True

        with patch("clipsave_app.main_window.QMessageBox.warning") as warning:
            self.window._ai_failed(ai_request[0], ai_request[1], item_id, "late ai")
            self.window._ocr_failed(ocr_request[0], ocr_request[1], item_id, "late ocr")

        warning.assert_not_called()

    def test_failed_session_shutdown_keeps_live_import_request_tracked(self):
        request = (object(), AsyncSignals())
        self.window._import_request = request
        ocr_request = (object(), AsyncSignals())
        current_id = self.window.current_items[0]["id"]
        self.window.select_item(current_id)
        self.window.toggle_detail()
        self.window._ocr_requests[current_id] = ocr_request

        def wait_request(candidate, _timeout, **_kwargs):
            return candidate is not request

        with patch.object(
            self.window, "_cancel_and_wait_request", side_effect=wait_request
        ), patch.object(self.window.detail, "set_ocr_busy") as set_ocr_busy:
            self.assertFalse(self.window.quit_application_for_session_end(0.2))

        self.assertIs(self.window._import_request, request)
        self.assertIs(self.window._ocr_requests[current_id], ocr_request)
        set_ocr_busy.assert_called_once_with(True)

    def test_async_task_timeout_keeps_ai_ocr_requests_tracked(self):
        item_id = self.window.current_items[0]["id"]
        ai_request = (object(), AsyncSignals())
        ocr_request = (object(), AsyncSignals())
        self.window._ai_requests[item_id] = ai_request
        self.window._ocr_requests[item_id] = ocr_request

        with patch.object(
            self.window, "_cancel_and_wait_for_async_tasks", return_value=False
        ):
            self.assertFalse(self.window.quit_application_for_session_end(0.2))

        self.assertIs(self.window._ai_requests[item_id], ai_request)
        self.assertIs(self.window._ocr_requests[item_id], ocr_request)

    def test_cancelled_bounded_request_is_cleaned_when_worker_finishes_late(self):
        item_id = self.window.current_items[0]["id"]
        token = object()
        signals = AsyncSignals()
        handle = Mock()
        handle.done_event = threading.Event()
        self.window._ocr_requests[item_id] = (token, signals)
        with self.window._async_tasks_lock:
            self.window._bounded_tasks[token] = handle

        self.window._schedule_cancelled_request_cleanup({token})
        self.app.processEvents()
        self.assertIn(item_id, self.window._ocr_requests)
        handle.done_event.set()
        deadline = time.monotonic() + 1
        while item_id in self.window._ocr_requests and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.02)

        self.assertNotIn(item_id, self.window._ocr_requests)
        with self.window._async_tasks_lock:
            self.assertNotIn(token, self.window._bounded_tasks)

    def test_session_abort_restores_semantic_button_after_completed_cancel(self):
        token = object()
        signals = AsyncSignals()
        self.window._semantic_request = (token, signals)
        self.window.semantic_button.setEnabled(False)
        self.window.semantic_button.setText("搜索中…")

        with patch.object(self.window.clipboard_service, "shutdown", return_value=False):
            self.assertFalse(self.window.quit_application_for_session_end(0.2))

        self.assertIsNone(self.window._semantic_request)
        self.assertTrue(self.window.semantic_button.isEnabled())
        self.assertEqual(self.window.semantic_button.text(), "语义搜索")

    def test_session_note_shutdown_does_not_synchronously_read_database(self):
        item_id = self.window.current_items[0]["id"]
        self.window.select_item(item_id)
        self.window.toggle_detail()
        self.window.detail.notes.setPlainText("pending")

        with patch.object(
            self.database, "get_item", side_effect=lambda *_args: time.sleep(0.2)
        ) as get_item, patch.object(
            self.database, "set_notes_if_unchanged", return_value=False
        ):
            started = time.monotonic()
            self.assertFalse(self.window.quit_application_for_session_end(0.05))
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.1)
        get_item.assert_not_called()

    def test_partial_session_note_success_clears_only_committed_drafts(self):
        first_id = self.window.current_items[0]["id"]
        second_id = self.database.add_text("second")
        self.window.detail._note_drafts[first_id] = "first draft"
        self.window.detail._note_draft_bases[first_id] = ""
        self.window.detail._note_drafts[second_id] = "second draft"
        self.window.detail._note_draft_bases[second_id] = ""

        with patch.object(
            self.database,
            "set_notes_if_unchanged",
            side_effect=[True, False],
        ):
            self.assertFalse(self.window.quit_application_for_session_end(0.2))

        drafts = self.window.detail.pending_note_drafts()
        self.assertNotIn(first_id, drafts)
        self.assertEqual(drafts[second_id], "second draft")

    def test_request_wait_uses_remaining_timeout_not_fixed_interval(self):
        token = object()
        signals = AsyncSignals()
        thread = threading.Thread(target=lambda: time.sleep(0.1), daemon=True)
        cancel_event = threading.Event()
        with self.window._async_tasks_lock:
            self.window._async_tasks[token] = (cancel_event, thread)
        thread.start()
        started = time.monotonic()
        result = self.window._cancel_and_wait_request(
            (token, signals), 0.001, process_events=False
        )
        elapsed = time.monotonic() - started
        thread.join(0.2)
        with self.window._async_tasks_lock:
            self.window._async_tasks.pop(token, None)

        self.assertFalse(result)
        self.assertLess(elapsed, 0.02)

    def test_failed_session_shutdown_restores_ai_ocr_button_state(self):
        image_path = Path(self.temp.name) / "busy-session.png"
        image = QImage(16, 16, QImage.Format.Format_RGB32)
        image.fill(QColor("#21a8fb"))
        self.assertTrue(image.save(str(image_path)))
        item_id = self.database.add_image(image_path)
        self.window.refresh_library()
        self.window.select_item(item_id)
        self.window.toggle_detail()
        request = (object(), AsyncSignals())
        self.window._ocr_requests[item_id] = request
        self.window.detail.set_ocr_busy(True)

        with patch.object(self.window.clipboard_service, "shutdown", return_value=False):
            self.assertFalse(self.window.quit_application_for_session_end(0.2))

        self.assertNotIn(item_id, self.window._ocr_requests)
        self.assertTrue(self.window.detail.ocr_button.isEnabled())
        self.assertNotIn("识别中", self.window.detail.ocr_button.text())

    def test_cancelled_import_reports_unprocessed_files_not_duplicates(self):
        token = object()
        signals = AsyncSignals()
        self.window._import_request = (token, signals)
        with patch.object(self.window, "show_status") as show_status:
            self.window._import_finished(
                token,
                signals,
                {
                    "total": 5,
                    "processed": 1,
                    "added": 1,
                    "localized": 0,
                    "duplicates": 0,
                    "failed": [],
                    "cancelled": True,
                },
            )

        message = show_status.call_args.args[0]
        self.assertIn("未处理 4 项", message)
        self.assertIn("跳过 0 项重复内容", message)

    def test_import_callbacks_do_not_show_modals_during_session_shutdown(self):
        token = object()
        signals = AsyncSignals()
        self.window._import_request = (token, signals)
        self.window._quit_in_progress = True
        with patch("clipsave_app.main_window.QMessageBox.warning") as warning:
            self.window._import_failed(token, signals, "late failure")
            self.window._import_finished(
                token,
                signals,
                {"total": 1, "added": 0, "localized": 0, "failed": [("x", "late")], "cancelled": True},
            )
        warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
