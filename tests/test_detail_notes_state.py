import unittest

from clipsave_app.detail_notes_state import DetailNotesState


class DetailNotesStateTests(unittest.TestCase):
    def test_stage_tracks_original_base_once(self):
        state = DetailNotesState()
        state.display_notes(7, "original")

        self.assertTrue(state.stage(7, "first edit"))
        self.assertTrue(state.stage(7, "second edit"))

        self.assertEqual(state.pending_updates()[7], ("original", "second edit"))

    def test_reverting_to_loaded_value_clears_failed_draft(self):
        state = DetailNotesState()
        state.display_notes(7, "original")
        state.drafts[7] = "failed"
        state.bases[7] = "original"

        self.assertFalse(state.stage(7, "original"))

        self.assertEqual(state.pending_drafts(), {})
        self.assertEqual(state.pending_updates(), {})

    def test_mark_saved_only_clears_matching_draft(self):
        state = DetailNotesState()
        state.display_notes(1, "old")
        state.stage(1, "draft")

        state.mark_saved(1, "other", update_loaded=False)
        self.assertEqual(state.pending_drafts()[1], "draft")

        state.mark_saved(1, "draft", update_loaded=True)
        self.assertEqual(state.loaded_notes, "draft")
        self.assertEqual(state.pending_drafts(), {})
