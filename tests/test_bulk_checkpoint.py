import tempfile
import unittest
from pathlib import Path

from clipsave_app.bulk_checkpoint import (
    clear_checkpoint,
    load_checkpoint,
    new_checkpoint,
    save_checkpoint,
)


class BulkImageCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "bulk-image-job.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_checkpoint_round_trip_preserves_description_resume_stage(self):
        checkpoint = new_checkpoint([11, 22, 33]).at_stage("description")

        save_checkpoint(self.path, checkpoint)
        loaded = load_checkpoint(self.path)

        self.assertEqual(loaded, checkpoint)
        self.assertEqual(loaded.current_item_id, 11)
        self.assertEqual(loaded.processed, 0)

    def test_advance_updates_exactly_one_outcome_and_resets_stage(self):
        checkpoint = new_checkpoint([11, 22]).at_stage("description")

        checkpoint = checkpoint.advance("completed")

        self.assertEqual(checkpoint.next_index, 1)
        self.assertEqual(checkpoint.completed, 1)
        self.assertEqual(checkpoint.stage, "ocr")
        self.assertEqual(checkpoint.current_item_id, 22)

    def test_invalid_checkpoint_is_rejected_and_clear_is_idempotent(self):
        self.path.write_text('{"version":1,"image_ids":[1]}', encoding="utf-8")

        with self.assertRaises(ValueError):
            load_checkpoint(self.path)

        clear_checkpoint(self.path)
        clear_checkpoint(self.path)
        self.assertIsNone(load_checkpoint(self.path))


if __name__ == "__main__":
    unittest.main()
