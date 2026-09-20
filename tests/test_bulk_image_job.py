import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from clipsave_app.bulk_checkpoint import load_checkpoint, new_checkpoint
from clipsave_app.bulk_image_job import BulkImageJob


class FakeDatabase:
    def __init__(self, item):
        self.item = item
        self.ocr_updates = []
        self.ai_updates = []

    def get_item(self, item_id):
        return self.item if item_id == self.item["id"] else None

    def update_ocr_if_current(self, item_id, digest, text):
        self.ocr_updates.append((item_id, digest, text))
        return True

    def update_ai_if_current(self, item_id, digest, text):
        self.ai_updates.append((item_id, digest, text))
        return True


class FakeSnapshot:
    def require_current(self):
        return None


class FakeService:
    def __init__(self, *, fail_description=False):
        self.fail_description = fail_description
        self.calls = []

    def ocr_image(self, _snapshot, _cancel, *, expected_sha256):
        self.calls.append(("ocr", expected_sha256))
        return "ocr"

    def describe_image(self, _snapshot, _cancel, *, expected_sha256):
        self.calls.append(("description", expected_sha256))
        if self.fail_description:
            raise RuntimeError("provider failed")
        return "description"


class BulkImageJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.checkpoint_path = Path(self.temp.name) / "bulk.json"
        self.item = {
            "id": 11,
            "kind": "image",
            "path": str(Path(self.temp.name) / "image.png"),
            "content_hash": "a" * 64,
        }

    def tearDown(self):
        self.temp.cleanup()

    @patch("clipsave_app.bulk_image_job.preflight_image_file", return_value=FakeSnapshot())
    def test_job_runs_ocr_then_description_and_completes_checkpoint(self, _preflight):
        database = FakeDatabase(self.item)
        service = FakeService()
        progress = []
        job = BulkImageJob(database, service, self.checkpoint_path)

        result = job.run(
            new_checkpoint([11]),
            threading.Event(),
            lambda *values: progress.append(values),
        )

        self.assertEqual(service.calls, [("ocr", "a" * 64), ("description", "a" * 64)])
        self.assertEqual(result.completed, 1)
        self.assertFalse(result.cancelled)
        self.assertEqual(load_checkpoint(self.checkpoint_path).processed, 1)
        self.assertEqual(progress[-1], (1, 1, 11, "已完成"))

    @patch("clipsave_app.bulk_image_job.preflight_image_file", return_value=FakeSnapshot())
    def test_description_failure_preserves_description_resume_stage(self, _preflight):
        database = FakeDatabase(self.item)
        job = BulkImageJob(database, FakeService(fail_description=True), self.checkpoint_path)

        result = job.run(new_checkpoint([11]), threading.Event(), lambda *_args: None)

        checkpoint = load_checkpoint(self.checkpoint_path)
        self.assertEqual(checkpoint.stage, "description")
        self.assertEqual(checkpoint.processed, 0)
        self.assertEqual(result.error, "provider failed")
