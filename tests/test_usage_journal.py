"""Usage journal append/parse contract tests."""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from clipsave_app.usage_journal import append_usage, journal_file, parse_usage


class UsageJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "Usage"

    def tearDown(self):
        self.temp.cleanup()

    def test_round_trip_through_local_offset(self):
        moment = dt.datetime(2026, 7, 21, 8, 0, 0, 123456, tzinfo=dt.timezone.utc)
        append_usage(self.directory, "2026-07-01T09:12:33Z", "ab" * 32, "标题", moment)
        entries = parse_usage(self.directory)
        self.assertEqual(len(entries), 1)
        captured_at, hash_prefix, used_at = entries[0]
        self.assertEqual(captured_at, "2026-07-01T09:12:33Z")
        self.assertEqual(hash_prefix, "ab" * 8)
        self.assertEqual(used_at, "2026-07-21T08:00:00.123456Z")

    def test_month_file_uses_local_month(self):
        # 2026-07-31 23:30 UTC is already August in UTC+8; the journal file
        # must match the local month the line was written in.
        moment = dt.datetime(2026, 7, 31, 23, 30, tzinfo=dt.timezone.utc)
        append_usage(self.directory, "2026-07-31T23:30:00Z", "cd" * 32, "x", moment)
        local_month = f"usage-{moment.astimezone():%Y-%m}.md"
        self.assertTrue((self.directory / local_month).is_file())

    def test_malformed_lines_are_skipped(self):
        append_usage(self.directory, "2026-07-01T09:12:33Z", "ab" * 32, "kept", None or dt.datetime.now(dt.timezone.utc))
        with journal_file(self.directory, dt.datetime.now().astimezone()).open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write("not a journal line\n")
            handle.write("garbage <- captured broken hash=zz\n")
        entries = parse_usage(self.directory)
        self.assertEqual(len(entries), 1)

    def test_space_separated_captured_timestamps_are_preserved(self):
        moment = dt.datetime.now(dt.timezone.utc)
        append_usage(self.directory, "2026-07-01 09:12:33", "ab" * 32, "manual", moment)
        entries = parse_usage(self.directory)
        self.assertEqual(entries[0][0], "2026-07-01 09:12:33")


if __name__ == "__main__":
    unittest.main()
