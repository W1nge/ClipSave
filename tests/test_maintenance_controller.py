import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from clipsave_app.maintenance_controller import LibraryMaintenanceController
from clipsave_app.task_supervisor import TaskSupervisor


class FakeDatabase:
    def __init__(self, *, dirty=True):
        self.scan_calls = []
        self.backups = 0
        self.dirty = dirty

    def mark_missing_files(self, cancel_event):
        self.scan_calls.append("missing")

    def scan_legacy_files(self, cancel_event):
        self.scan_calls.append("legacy")
        return 3

    def scan_unindexed_files(self, cancel_event):
        self.scan_calls.append("unindexed")
        return 2

    def create_backup_if_changed(self):
        self.backups += 1
        return "backup.db"

    def backup_state(self):
        return {"dirty": self.dirty}


class MaintenanceControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_for(self, predicate, timeout=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        return False

    def test_startup_scan_runs_database_maintenance_off_caller(self):
        database = FakeDatabase()
        controller = LibraryMaintenanceController(database, TaskSupervisor())
        results = []
        controller.scan_succeeded.connect(lambda token, marker, count: results.append((token, marker, count)))

        request = controller.start_scan(True, False)

        self.assertTrue(self.wait_for(lambda: bool(results)))
        self.assertEqual(database.scan_calls, ["missing", "legacy"])
        self.assertEqual(results[0][2], 3)
        self.assertTrue(controller.finish_scan(*request))

    def test_backup_request_has_single_owner(self):
        database = FakeDatabase()
        controller = LibraryMaintenanceController(database, TaskSupervisor())
        results = []
        controller.backup_succeeded.connect(lambda token, marker, path: results.append((token, marker, path)))

        request = controller.start_backup()

        self.assertTrue(self.wait_for(lambda: bool(results)))
        self.assertEqual(database.backups, 1)
        self.assertEqual(results[0][2], "backup.db")
        self.assertTrue(controller.finish_backup(*request))

    def test_periodic_backup_starts_only_when_dirty_and_idle(self):
        clean = FakeDatabase(dirty=False)
        clean_controller = LibraryMaintenanceController(clean, TaskSupervisor())
        self.assertIsNone(clean_controller.start_backup_if_dirty())
        self.assertEqual(clean.backups, 0)

        dirty = FakeDatabase(dirty=True)
        dirty_controller = LibraryMaintenanceController(dirty, TaskSupervisor())
        request = dirty_controller.start_backup_if_dirty()
        self.assertIsNotNone(request)
        self.assertIs(dirty_controller.start_backup_if_dirty(), None)
        self.assertTrue(self.wait_for(lambda: dirty.backups == 1))
