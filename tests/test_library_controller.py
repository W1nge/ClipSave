import os
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from clipsave_app.library_controller import LibraryController
from clipsave_app.library_models import LibraryQuery
from clipsave_app.task_supervisor import TaskSupervisor


class FakeDatabase:
    def query_items(self, **kwargs):
        return [(kwargs["query"], kwargs["offset"])]

    def count_query_items(self, **kwargs):
        return 1

    def counts(self):
        return {"all": 1}

    def collections(self):
        return []

    def tags(self):
        return []

    def days(self):
        return [("2026-09-20", 1)]


class LibraryControllerTests(unittest.TestCase):
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
        self.app.processEvents()
        return bool(predicate())

    def test_search_owns_request_and_emits_typed_query_result(self):
        supervisor = TaskSupervisor()
        controller = LibraryController(FakeDatabase(), supervisor)
        results = []
        controller.search_succeeded.connect(lambda token, query, items: results.append((token, query, items)))

        query = LibraryQuery(query="needle")
        controller.search(query, 10)

        self.assertIsNotNone(controller.search_request)
        self.assertTrue(self.wait_for(lambda: bool(results)))
        token, returned_query, payload = results[0]
        self.assertEqual(returned_query, query)
        self.assertEqual(payload.items, [("needle", 0)])
        self.assertEqual(payload.total, 1)
        self.assertTrue(controller.finish_search(token))
        self.assertIsNone(controller.search_request)

    def test_cancel_search_clears_request_and_suppresses_running_result(self):
        entered = threading.Event()
        release = threading.Event()
        results = []

        class BlockingDatabase(FakeDatabase):
            def query_items(self, **kwargs):
                entered.set()
                release.wait(1.0)
                return []

        supervisor = TaskSupervisor()
        controller = LibraryController(BlockingDatabase(), supervisor)
        controller.search_succeeded.connect(lambda *args: results.append(args))
        controller.search(LibraryQuery(query="slow"), 10)
        self.assertTrue(entered.wait(1.0))
        self.assertIsNotNone(controller.search_request)

        controller.cancel_search()

        self.assertIsNone(controller.search_request)
        release.set()
        deadline = time.monotonic() + 1.0
        while supervisor.regular_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertFalse(supervisor.regular_tasks)
        self.assertEqual(results, [])

    def test_replaced_search_keeps_only_latest_pending_query(self):
        first_started = threading.Event()
        first_release = threading.Event()

        class BlockingDatabase(FakeDatabase):
            def __init__(self):
                self.calls = []

            def query_items(self, **kwargs):
                query = kwargs["query"]
                self.calls.append(query)
                if query == "q0":
                    first_started.set()
                    first_release.wait(1.0)
                return [(query, kwargs["offset"])]

        database = BlockingDatabase()
        supervisor = TaskSupervisor()
        controller = LibraryController(database, supervisor)
        results = []
        controller.search_succeeded.connect(
            lambda token, query, items: results.append((token, query, items))
        )

        controller.search(LibraryQuery(query="q0"), 10)
        self.assertTrue(first_started.wait(1.0))
        for index in range(1, 5):
            controller.search(LibraryQuery(query=f"q{index}"), 10)

        self.assertEqual(len(supervisor.regular_tasks), 1)
        first_release.set()
        self.assertTrue(self.wait_for(lambda: bool(results)))

        self.assertEqual(database.calls, ["q0", "q4"])
        self.assertEqual(results[0][1].query, "q4")
        self.assertEqual(results[0][2].items, [("q4", 0)])
        self.assertEqual(results[0][2].total, 1)
        self.assertTrue(controller.finish_search(results[0][0]))
        deadline = time.monotonic() + 1.0
        while supervisor.regular_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertFalse(supervisor.regular_tasks)

    def test_snapshot_is_shared_by_sync_and_async_refresh_paths(self):
        controller = LibraryController(FakeDatabase(), TaskSupervisor())
        query = LibraryQuery(query="needle")

        snapshot = controller.snapshot(query, 10)

        self.assertEqual(snapshot.navigation.counts, {"all": 1})
        self.assertEqual(snapshot.navigation.days, [("2026-09-20", 1)])
        self.assertEqual(snapshot.items, [("needle", 0)])
        self.assertEqual(snapshot.total, 1)

    def test_search_start_failure_reports_error_and_returns_to_idle(self):
        supervisor = TaskSupervisor()
        controller = LibraryController(FakeDatabase(), supervisor)
        failures = []
        controller.search_failed.connect(lambda token, message: failures.append((token, message)))

        with patch.object(supervisor, "start_thread", side_effect=RuntimeError("no threads")):
            controller.search(LibraryQuery(query="needle"), 10)

        self.assertIsNone(controller.search_request)
        self.assertEqual(len(failures), 1)
        self.assertIn("no threads", failures[0][1])
