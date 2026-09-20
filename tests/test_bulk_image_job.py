import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from clipsave_app.bulk_checkpoint import load_checkpoint, new_checkpoint, save_checkpoint
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
        self.source_roots = []

    def ocr_image(self, _snapshot, _cancel, *, expected_sha256, source_root=None):
        self.calls.append(("ocr", expected_sha256))
        self.source_roots.append(source_root)
        return "ocr"

    def describe_image(self, _snapshot, _cancel, *, expected_sha256, source_root=None):
        self.calls.append(("description", expected_sha256))
        self.source_roots.append(source_root)
        if self.fail_description:
            raise RuntimeError("provider failed")
        return "description"


class RecordingCoordinator:
    def __init__(self):
        self.calls = []

    def run_bounded(
        self,
        item_id,
        operation,
        target,
        *,
        estimated_bytes,
        cancel_event,
    ):
        self.calls.append((item_id, operation, estimated_bytes))
        return target(cancel_event)


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

    @patch("clipsave_app.bulk_image_job.preflight_image_file", return_value=FakeSnapshot())
    def test_external_image_uses_parent_as_identity_root(self, _preflight):
        item = dict(self.item)
        item["path"] = str(Path(self.temp.name) / "External" / "image.png")
        item["external"] = 1
        database = FakeDatabase(item)
        service = FakeService()
        job = BulkImageJob(database, service, self.checkpoint_path)

        result = job.run(new_checkpoint([11]), threading.Event(), lambda *_args: None)

        self.assertEqual(result.completed, 1)
        expected_root = Path(item["path"]).parent
        self.assertEqual(service.source_roots, [expected_root, expected_root])

    @patch("clipsave_app.bulk_image_job.preflight_image_file", return_value=FakeSnapshot())
    def test_bulk_stages_reserve_decoded_image_bytes_through_coordinator(self, _preflight):
        item = dict(self.item)
        item["width"] = 20
        item["height"] = 10
        database = FakeDatabase(item)
        coordinator = RecordingCoordinator()
        job = BulkImageJob(
            database,
            FakeService(),
            self.checkpoint_path,
            coordinator,
        )

        result = job.run(new_checkpoint([11]), threading.Event(), lambda *_args: None)

        self.assertEqual(result.completed, 1)
        self.assertEqual(
            coordinator.calls,
            [(11, "ocr", 800), (11, "ai", 800)],
        )

    @patch("clipsave_app.bulk_image_job.preflight_image_file", return_value=FakeSnapshot())
    def test_ocr_provider_result_is_reused_after_stage_checkpoint_failure(self, _preflight):
        database = FakeDatabase(self.item)
        first_service = FakeService()
        coordinator = RecordingCoordinator()
        real_save = save_checkpoint
        save_calls = 0

        def fail_second_save(path, checkpoint):
            nonlocal save_calls
            save_calls += 1
            if save_calls == 2:
                raise OSError("checkpoint unavailable")
            real_save(path, checkpoint)

        with patch("clipsave_app.bulk_image_job.save_checkpoint", side_effect=fail_second_save):
            first_result = BulkImageJob(
                database,
                first_service,
                self.checkpoint_path,
                coordinator,
            ).run(new_checkpoint([11]), threading.Event(), lambda *_args: None)

        persisted = load_checkpoint(self.checkpoint_path)
        self.assertEqual(first_result.error, "checkpoint unavailable")
        self.assertEqual(persisted.stage, "ocr")
        self.assertEqual(persisted.pending_stage, "ocr")
        self.assertEqual(persisted.pending_text, "ocr")
        self.assertEqual(database.ocr_updates, [(11, "a" * 64, "ocr")])

        resumed_service = FakeService()
        resumed = BulkImageJob(
            database,
            resumed_service,
            self.checkpoint_path,
            RecordingCoordinator(),
        ).run(persisted, threading.Event(), lambda *_args: None)

        self.assertEqual(resumed.completed, 1)
        self.assertEqual(resumed_service.calls, [("description", "a" * 64)])
        self.assertEqual(len(database.ocr_updates), 2)

    @patch("clipsave_app.bulk_image_job.preflight_image_file", return_value=FakeSnapshot())
    def test_description_provider_result_is_reused_after_completion_checkpoint_failure(self, _preflight):
        database = FakeDatabase(self.item)
        first_service = FakeService()
        real_save = save_checkpoint
        save_calls = 0

        def fail_fourth_save(path, checkpoint):
            nonlocal save_calls
            save_calls += 1
            if save_calls == 4:
                raise OSError("checkpoint unavailable")
            real_save(path, checkpoint)

        with patch("clipsave_app.bulk_image_job.save_checkpoint", side_effect=fail_fourth_save):
            first_result = BulkImageJob(
                database,
                first_service,
                self.checkpoint_path,
                RecordingCoordinator(),
            ).run(new_checkpoint([11]), threading.Event(), lambda *_args: None)

        persisted = load_checkpoint(self.checkpoint_path)
        self.assertEqual(first_result.error, "无法保存批处理断点：checkpoint unavailable")
        self.assertEqual(persisted.stage, "description")
        self.assertEqual(persisted.pending_stage, "description")
        self.assertEqual(persisted.pending_text, "description")
        self.assertEqual(database.ai_updates, [(11, "a" * 64, "description")])

        resumed_service = FakeService()
        resumed = BulkImageJob(
            database,
            resumed_service,
            self.checkpoint_path,
            RecordingCoordinator(),
        ).run(persisted, threading.Event(), lambda *_args: None)

        self.assertEqual(resumed.completed, 1)
        self.assertEqual(resumed_service.calls, [])
        self.assertEqual(len(database.ai_updates), 2)
