"""Tests for the local run history store, using a temporary folder."""

import tempfile
import unittest
from pathlib import Path

from aether_ui import history
from aether_ui.workflow import run_workflow


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "history.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_missing_file_means_empty_history(self):
        self.assertEqual(history.load_history(self.path), [])

    def test_save_and_reopen_run(self):
        result = run_workflow("A website with login")
        history.save_run(result, self.path)

        entries = history.load_history(self.path)
        self.assertEqual(len(entries), 1)
        self.assertEqual(history.entry_to_result(entries[0]).plan, result.plan)

    def test_newest_first_and_limited(self):
        for i in range(history.MAX_ENTRIES + 3):
            history.save_run(run_workflow(f"Requirement number {i}"), self.path)

        entries = history.load_history(self.path)
        self.assertEqual(len(entries), history.MAX_ENTRIES)
        newest = history.entry_to_result(entries[0])
        self.assertEqual(newest.requirement, f"Requirement number {history.MAX_ENTRIES + 2}")

    def test_damaged_file_is_ignored(self):
        self.path.write_text("not json", encoding="utf-8")
        self.assertEqual(history.load_history(self.path), [])

    def test_clear_history(self):
        history.save_run(run_workflow("A website"), self.path)
        history.clear_history(self.path)
        self.assertEqual(history.load_history(self.path), [])


if __name__ == "__main__":
    unittest.main()
