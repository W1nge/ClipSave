import os
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from clipsave_app.library_controller import LibraryController
from clipsave_app.library_models import LibraryQuery
from clipsave_app.task_supervisor import TaskSupervisor


class FakeDatabase:
    def query_items(self, **kwargs):
        return [(kwargs["query"], kwargs["offset"])]

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
        token, returned_query, items = results[0]
        self.assertEqual(returned_query, query)
        self.assertEqual(items, [("needle", 0)])
        self.assertTrue(controller.finish_search(token))
        self.assertIsNone(controller.search_request)

    def test_cancel_search_clears_request_and_cancels_task(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingDatabase(FakeDatabase):
            def query_items(self, **kwargs):
                entered.set()
                release.wait(1.0)
                return []

        supervisor = TaskSupervisor()
        controller = LibraryController(BlockingDatabase(), supervisor)
        controller.search(LibraryQuery(query="slow"), 10)
        self.assertTrue(entered.wait(1.0))
        request = controller.search_request
        self.assertIsNotNone(request)

        controller.cancel_search()

        self.assertIsNone(controller.search_request)
        release.set()
        self.assertTrue(supervisor.wait_for_token(request.token, 1.0))

    def test_snapshot_is_shared_by_sync_and_async_refresh_paths(self):
        controller = LibraryController(FakeDatabase(), TaskSupervisor())
        query = LibraryQuery(query="needle")

        snapshot = controller.snapshot(query, 10)

        self.assertEqual(snapshot.navigation.counts, {"all": 1})
        self.assertEqual(snapshot.navigation.days, [("2026-09-20", 1)])
        self.assertEqual(snapshot.items, [("needle", 0)])
