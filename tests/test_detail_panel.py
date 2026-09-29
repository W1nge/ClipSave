import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QFocusEvent, QTextCursor, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from clipsave_app.detail_panel import DetailPanel, _DetailMetadataValue
from clipsave_app.detail_tag_grid import DetailTagChip
from clipsave_app.styles import stylesheet_for_theme


def record(**changes):
    item = dict(
        id=1, title="一小段内容", content="一小段内容", kind="text", path=None,
        width=0, height=0, file_size=15, source="剪贴板", favorite=0,
        created_at="2026-09-29T01:05:37", last_used_at="2026-09-29T01:05:37",
        collection_id=None, tag_names="", tag_colors="", notes="",
        ai_description="", ocr_text="",
    )
    item.update(changes)
    return item


class DetailPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.app.setStyleSheet(stylesheet_for_theme(True))
        self.panel = DetailPanel()
        self.panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.panel.resize(340, 640)
        self.panel.set_item(record())
        self.panel.show()
        self.settle()

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()
        self.app.setStyleSheet("")

    def settle(self):
        for _ in range(5):
            self.app.processEvents()
            QTest.qWait(5)

    def test_short_content_has_no_duplicate_heading_or_image_tools(self):
        self.assertTrue(self.panel.title.isHidden())
        self.assertTrue(self.panel.image_tools.isHidden())
        self.assertLess(self.panel.preview_stack.height(), 110)
        self.assertEqual(self.panel.text_preview.toPlainText(), "一小段内容")
        self.assertTrue(self.panel.tags_empty.isVisible())
        self.assertEqual(self.panel.verticalScrollBar().maximum(), 0)
        self.panel.set_item(record(title="有意义的自定义标题"))
        self.assertFalse(self.panel.title.isHidden())

    def test_long_tags_fit_narrow_details_and_only_remove_button_removes(self):
        name = "产品设计讨论记录以及下一次版本发布计划"
        self.panel.set_item(record(tag_names=name + '\x1f' + name, tag_colors='#65b697\x1f#589ede'))
        removed = []
        self.panel.remove_tag_requested.connect(lambda *args: removed.append(args))
        for width in (340, 280):
            self.panel.resize(width, 700)
            self.settle()
            self.assertLessEqual(self.panel.content_widget.width(), self.panel.viewport().width())
            self.assertEqual(self.panel.horizontalScrollBar().maximum(), 0)
            chips = self.panel.tag_grid.findChildren(DetailTagChip)
            for chip in chips:
                self.assertTrue(self.panel.tag_grid.rect().contains(chip.geometry()))
                self.assertEqual(chip.label.text(), name)
                self.assertEqual(chip.label.toolTip(), name)
                self.assertIn('…', chip.label.display_text())
                self.assertLessEqual(chip.label.fontMetrics().horizontalAdvance(chip.label.display_text()), chip.label.width())
                QTest.mouseClick(chip.label, Qt.MouseButton.LeftButton)
            self.assertEqual(removed, [])
        QTest.mouseClick(chips[0].remove_button, Qt.MouseButton.LeftButton)
        self.assertEqual(removed, [(1, name)])

    def test_metadata_refresh_preserves_preview_position_and_selection(self):
        for kind in ('text', 'markdown'):
            with self.subTest(kind=kind):
                item = record(kind=kind, content='第一段内容。\n\n' * 150)
                self.panel.set_item(item)
                self.settle()
                browser = self.panel.text_preview
                cursor = browser.textCursor()
                cursor.setPosition(20)
                cursor.setPosition(30, QTextCursor.MoveMode.KeepAnchor)
                browser.setTextCursor(cursor)
                browser.verticalScrollBar().setValue(browser.verticalScrollBar().maximum() // 2)
                position = browser.verticalScrollBar().value()
                selected = browser.textCursor().selectedText()
                self.assertGreater(position, 0)
                self.panel.set_item(dict(item, favorite=1, collection_id=2, tag_names='产品设计'))
                self.settle()
                self.assertEqual(browser.verticalScrollBar().value(), position)
                self.assertEqual(browser.textCursor().selectedText(), selected)
                self.panel.set_item(dict(item, id=2))
                self.settle()
                self.assertEqual(browser.verticalScrollBar().value(), 0)
                self.assertFalse(browser.textCursor().hasSelection())
                browser.verticalScrollBar().setValue(browser.verticalScrollBar().maximum() // 2)
                self.panel.set_item(dict(item, id=2, content='新内容\n\n' * 150))
                self.settle()
                self.assertEqual(browser.verticalScrollBar().value(), 0)

    def test_collection_popup_anchors_selects_by_click_and_closes_on_scroll(self):
        self.panel.set_collections([dict(id=1, name='Work'), dict(id=2, name='Reference')])
        self.panel.resize(280, 400)
        self.settle()
        combo = self.panel.collection_combo
        self.panel.ensureWidgetVisible(combo)
        changes = []
        self.panel.collection_changed.connect(lambda item_id, value: changes.append((item_id, value)))
        combo.showPopup()
        popup = combo.collection_popup
        self.settle()
        self.assertTrue(popup.isVisible())
        self.assertEqual(popup.width(), combo.width())
        self.assertFalse(popup.isWindow())
        self.assertFalse(popup.testAttribute(Qt.WidgetAttribute.WA_NativeWindow))
        self.assertEqual(popup.pos(), combo.mapTo(self.panel, QPoint(0, combo.height())))
        index = combo.model().index(1, 0)
        QTest.mouseClick(combo.collection_list.viewport(), Qt.MouseButton.LeftButton,
                         pos=combo.collection_list.visualRect(index).center())
        self.assertEqual(combo.currentData(), 1)
        self.assertEqual(changes, [(1, 1)])
        combo.showPopup()
        self.panel.verticalScrollBar().setValue(self.panel.verticalScrollBar().maximum())
        self.assertFalse(popup.isVisible())
        self.panel.ensureWidgetVisible(combo)
        combo.showPopup()
        self.panel.resize(300, 440)
        self.assertFalse(popup.isVisible())

    def test_long_markdown_starts_at_top_and_is_fully_scrollable(self):
        content = "# 标题\n\n" + "第一段内容。\n\n" * 150 + "最后一段。"
        self.panel.set_item(record(kind="markdown", content=content, title="# 标题"))
        self.settle()
        browser = self.panel.text_preview
        self.assertTrue(self.panel.title.isHidden())
        self.assertEqual(self.panel.preview_stack.height(), 280)
        self.assertEqual(browser.verticalScrollBar().value(), 0)
        self.assertGreater(browser.verticalScrollBar().maximum(), 0)
        browser.verticalScrollBar().setValue(browser.verticalScrollBar().maximum())
        self.assertIn("最后一段", browser.toPlainText())
        self.assertGreaterEqual(browser.cursorForPosition(browser.viewport().rect().bottomLeft()).blockNumber(), 148)

    def test_embedded_collection_list_scroll_keyboard_and_outside_dismiss(self):
        self.panel.set_collections([dict(id=i, name=f'Collection {i}') for i in range(1, 31)])
        combo = self.panel.collection_combo
        combo.showPopup()
        self.settle()
        view = combo.collection_list
        popup = combo.collection_popup
        self.assertTrue(self.panel.rect().contains(popup.geometry()))
        event = QWheelEvent(QPointF(10, 10), QPointF(view.viewport().mapToGlobal(QPoint(10, 10))),
                            QPoint(), QPoint(0, -120), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        self.app.sendEvent(view.viewport(), event)
        self.assertGreater(view.verticalScrollBar().value(), 0)
        self.assertIsNone(combo.currentData())
        QTest.keyClick(view, Qt.Key.Key_Down)
        QTest.keyClick(view, Qt.Key.Key_Return)
        self.assertEqual(combo.currentData(), 1)
        self.assertFalse(popup.isVisible())
        combo.showPopup()
        QTest.keyClick(view, Qt.Key.Key_Escape)
        self.assertFalse(popup.isVisible())
        combo.showPopup()
        QTest.mouseClick(self.panel.copy_button, Qt.MouseButton.LeftButton)
        self.assertFalse(popup.isVisible())
        combo.showPopup()
        self.assertTrue(popup.isVisible())
        self.app.sendEvent(self.panel, QEvent(QEvent.Type.WindowDeactivate))
        self.assertFalse(popup.isVisible())

    def test_header_actions_remain_clickable_after_scroll_and_resize(self):
        copied, opened, deleted, favorites = [], [], [], []
        self.panel.copy_requested.connect(copied.append)
        self.panel.open_requested.connect(opened.append)
        self.panel.delete_requested.connect(deleted.append)
        self.panel.favorite_requested.connect(lambda item_id, value: favorites.append((item_id, value)))
        for width in (340, 280, 520):
            self.panel.setMaximumWidth(520)
            self.panel.resize(width, 300)
            self.settle()
            self.panel.verticalScrollBar().setValue(self.panel.verticalScrollBar().maximum())
            self.settle()
            for button in self.panel.item_action_buttons:
                center = button.mapTo(self.panel, button.rect().center())
                self.assertLess(center.y(), self.panel.viewport().geometry().top())
                self.assertIs(self.panel.childAt(center), button)
                QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        self.assertEqual(copied, [1, 1, 1])
        self.assertEqual(opened, [1, 1, 1])
        self.assertEqual(deleted, [1, 1, 1])
        self.assertEqual(favorites, [(1, True)] * 3)

    def test_preview_reflows_at_every_width_without_clipping_text(self):
        content = "多行内容自动换行。" * 8
        self.panel.set_item(record(title=content[:80], content=content))
        heights = []
        self.panel.setMaximumWidth(520)
        for width in (520, 340, 280):
            self.panel.resize(width, 640)
            self.settle()
            heights.append(self.panel.preview_stack.height())
            self.assertEqual(self.panel.horizontalScrollBar().maximum(), 0)
            self.assertEqual(self.panel.text_preview.horizontalScrollBar().maximum(), 0)
            self.assertEqual(self.panel.text_preview.verticalScrollBar().maximum(), 0)
        self.assertEqual(heights, sorted(heights))
        self.assertGreater(heights[-1], heights[0])

    def test_metadata_can_expand_at_narrow_width_and_preserves_exact_path(self):
        path = "C:/captures/" + "长文件名_" * 35 + ".png"
        self.panel.set_item(record(kind="image", path=path, title="文件名.png"))
        self.panel.resize(280, 400)
        self.panel.info_toggle.click()
        self.settle()
        self.assertFalse(self.panel.meta.isHidden())
        self.assertIn(path, self.panel.meta.text())
        path_widget = next(widget for widget in self.panel.meta.findChildren(_DetailMetadataValue)
                           if widget.toPlainText() == path)
        self.assertEqual(path_widget.horizontalScrollBar().maximum(), 0)
        self.assertEqual(path_widget.verticalScrollBar().maximum(), 0)
        self.assertGreaterEqual(path_widget.cursorForPosition(path_widget.viewport().rect().bottomRight()).position(), len(path) - 1)
        self.assertEqual(self.panel.horizontalScrollBar().maximum(), 0)
        self.assertLessEqual(self.panel.content_widget.width(), self.panel.viewport().width())
        self.assertFalse(self.panel.copy_button.isEnabled())
        self.assertIn("移动或删除", self.panel.image_preview.text())
        self.panel.info_toggle.click()
        self.assertTrue(self.panel.meta.isHidden())

    def test_image_tools_show_saved_results_without_empty_placeholders(self):
        self.panel.set_item(record(kind="image", ocr_text="可选择的识别结果"))
        self.assertFalse(self.panel.image_tools.isHidden())
        self.assertFalse(self.panel.ocr_text.isHidden())
        self.assertTrue(self.panel.ai_description.isHidden())
        self.panel.set_item(record(id=2))
        self.assertTrue(self.panel.image_tools.isHidden())
        self.panel.clear_item()
        self.assertTrue(self.panel.preview_section.isHidden())
        self.assertTrue(all(not button.isEnabled() for button in self.panel.item_action_buttons))

    def test_notes_save_on_focus_loss_after_editing_and_resizing(self):
        saved = []
        self.panel.notes_changed.connect(lambda item_id, value: saved.append((item_id, value)))
        self.panel.notes.setFocus()
        self.panel.notes.insertPlainText("中文备注 English")
        self.panel.resize(280, 480)
        self.settle()
        # Hidden native windows cannot become the system's keyboard focus.
        # Deliver the same FocusOut event used by the composition input host.
        self.app.sendEvent(self.panel.notes, QFocusEvent(QEvent.Type.FocusOut))
        self.settle()
        self.assertEqual(saved, [(1, "中文备注 English")])
        self.assertEqual(self.panel.pending_note_drafts(), {1: "中文备注 English"})
        self.panel.mark_notes_saved(1, "中文备注 English")
        self.assertEqual(self.panel.pending_note_drafts(), {})
