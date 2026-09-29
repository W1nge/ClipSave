import unittest

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from clipsave_app.detail_tag_grid import DetailTagGrid


class DetailTagGridTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._grids = []

    def tearDown(self):
        for grid in self._grids:
            grid.close()
            grid.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def make_grid(self):
        grid = DetailTagGrid()
        self._grids.append(grid)
        return grid

    def test_more_button_expands_and_clear_resets_state(self):
        grid = self.make_grid()
        grid.set_tags(
            "one\x1ftwo\x1fthree\x1ffour\x1ffive\x1fsix",
            "#1\x1f#2\x1f#3\x1f#4\x1f#5\x1f#6",
        )

        self.assertIsNotNone(grid.more_button)
        grid.more_button.click()
        self.assertTrue(grid.expanded)
        self.assertEqual(
            sum(
                1
                for index in range(grid.grid.count())
                if grid.grid.itemAt(index).widget().objectName() == "TagChip"
            ),
            6,
        )

        grid.clear_tags()
        self.assertFalse(grid.expanded)
        self.assertEqual(grid.grid.count(), 0)

    def test_remove_signal_reports_tag_name(self):
        grid = self.make_grid()
        removed = []
        grid.remove_requested.connect(removed.append)
        grid.set_tags("work", "#64748b")

        grid.grid.itemAt(0).widget().remove_button.click()

        self.assertEqual(removed, ["work"])
