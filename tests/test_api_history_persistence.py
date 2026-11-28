"""FastAPI regression tests for the hardened history (Task 013): the read-only API still reports history honestly
while writers are busy, failing, or racing, and it works with files written by the new save path."""

import contextlib
import json
import os
import threading
import unittest
from pathlib import Path
from unittest import mock

try:
    from api_support import ApiTestCase, snapshot_tree
except ImportError as error:
    if "requirements-api.txt" in str(error):
        raise
    from tests.api_support import ApiTestCase, snapshot_tree

from aether import fileio
from aether_ui import history
from aether_ui.workflow import result_to_dict, run_workflow


class ApiHistoryPersistenceTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        for name, value in (("RETRY_DELAY", 0.0), ("LOCK_POLL", 0.005)):
            patcher = mock.patch.object(fileio, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def save(self, label: str) -> dict:
        return history.save_run(run_workflow(label))  # the history file comes from the isolated environment

    @contextlib.contextmanager
    def history_busy(self):
        release, held = threading.Event(), threading.Event()

        def holder():
            with fileio.exclusive_lock(history.lock_path(self.history_file)):
                held.set()
                release.wait(30)

        thread = threading.Thread(target=holder)
        thread.start()
        try:
            self.assertTrue(held.wait(5))
            yield
        finally:
            release.set()
            thread.join()

    @contextlib.contextmanager
    def history_unreadable(self):
        real = Path.read_text

        def blocked(path, *args, **kwargs):
            if Path(path) == self.history_file:
                raise PermissionError("another program has the history open")
            return real(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", blocked), mock.patch("aether_api.repository.time.sleep"):
            yield

    # ------------------------------------------------------------------ concurrent writers

    def test_the_api_lists_every_entry_that_concurrent_writers_saved(self):
        real = history._read_for_update

        def slow(path):
            found = real(path)
            threading.Event().wait(0.02)  # widen the read-to-write window
            return found

        with mock.patch.object(history, "_read_for_update", slow):
            threads = [threading.Thread(target=self.save, args=(f"writer {n}",)) for n in range(10)]
            [t.start() for t in threads]
            [t.join() for t in threads]

        body = self.get("/history").json()
        self.assertEqual((body["file_state"], body["problems"]), ("ok", []))
        self.assertEqual(sorted(e["requirement"] for e in body["entries"]), sorted(f"writer {n}" for n in range(10)))

    def test_reading_while_writers_are_saving_never_sees_a_damaged_or_unreadable_history(self):
        patcher = mock.patch.object(fileio, "RETRY_DELAY", 0.05)  # the real retry pause, not the fast test setting
        patcher.start()
        self.addCleanup(patcher.stop)
        states, counts = set(), []
        writers = [threading.Thread(target=lambda n=n: [self.save(f"w{n}-{i}") for i in range(3)]) for n in range(4)]
        [t.start() for t in writers]
        while any(t.is_alive() for t in writers):
            response = self.get("/history")
            self.assertEqual(response.status_code, 200)
            body = response.json()
            states.add(body["file_state"])
            counts.append(len(body["entries"]))
        [t.join() for t in writers]
        final = self.get("/history").json()
        self.assertTrue(states <= {"missing", "ok"}, states)
        self.assertEqual((final["file_state"], len(final["entries"])), ("ok", 12))
        self.assertEqual(counts, sorted(counts), "the history only grows, so reads never go backwards")

    def test_a_save_between_the_two_reads_of_the_history_is_not_reported_as_unreadable(self):
        """Regression: read_history() reads the file twice (count, then load). A writer landing in between used to make
        the counts differ, and a perfectly healthy history was reported as `unreadable` (seen once in a loaded full run)."""
        self.save("first")
        real, loads = history.load_history_report, []

        def load_after_a_save(path=None):
            if not loads:
                self.save("second")  # a writer lands exactly between the probe and the load
            loads.append(path)
            return real(path)

        with mock.patch.object(history, "load_history_report", load_after_a_save):
            body = self.get("/history").json()
        self.assertEqual((body["file_state"], body["problems"]), ("ok", []))
        self.assertEqual(sorted(e["requirement"] for e in body["entries"]), ["first", "second"])
        self.assertEqual(len(loads), 2, "the repository read again instead of giving up")

    def test_a_history_that_keeps_changing_is_still_reported_honestly_after_a_few_tries(self):
        self.save("first")
        real, loads = history.load_history_report, []

        def load_after_every_save(path=None):
            self.save(f"again {len(loads)}")  # the file changes between the probe and the load, every single time
            loads.append(path)
            return real(path)

        with mock.patch.object(history, "load_history_report", load_after_every_save):
            body = self.get("/history").json()
        self.assertEqual((body["file_state"], body["entries"]), ("unreadable", []))
        self.assertEqual(len(loads), 3, "a bounded number of tries, then the truth")

    def test_the_api_can_read_while_another_operation_holds_the_history_lock(self):
        self.save("a")
        before = snapshot_tree(self.base)
        with self.history_busy():
            body = self.get("/history").json()
            self.assertEqual(self.get("/config").status_code, 200)
        self.assertEqual((body["file_state"], len(body["entries"])), ("ok", 1))
        self.assertEqual(snapshot_tree(self.base), before, "reading neither waits for nor changes the lock")

    def test_reading_creates_no_lock_file(self):
        self.assertEqual(self.get("/history").json()["file_state"], "missing")
        self.get("/config")
        self.assertEqual(sorted(p.name for p in self.base.iterdir()), [])

    # ------------------------------------------------------------------ failures

    def test_a_failed_save_leaves_the_history_the_api_shows_unchanged(self):
        self.save("a")
        self.save("b")
        before = self.history_file.read_bytes()
        with mock.patch.object(fileio, "REPLACE_ATTEMPTS", 1), \
                mock.patch.object(fileio.os, "replace", side_effect=PermissionError("held open")):
            with self.assertRaises(history.HistoryWriteError):
                self.save("c")
        body = self.get("/history").json()
        self.assertEqual((body["file_state"], [e["requirement"] for e in body["entries"]]), ("ok", ["b", "a"]))
        self.assertEqual(self.history_file.read_bytes(), before)

    def test_an_unreadable_history_is_reported_by_the_api_and_refused_by_the_save(self):
        """End to end for the data-loss case: both sides agree it is unreadable, and nothing is overwritten."""
        self.save("a")
        self.save("b")
        before = self.history_file.read_bytes()
        with self.history_unreadable():
            self.assertEqual(self.get("/history").json()["file_state"], "unreadable")
            with self.assertRaises(history.HistoryReadError):
                self.save("c")
        self.assertEqual(self.history_file.read_bytes(), before)
        self.assertEqual(list(self.base.glob("history.json.damaged-*")), [])
        body = self.get("/history").json()  # once the problem is gone the original entries are all still there
        self.assertEqual([e["requirement"] for e in body["entries"]], ["b", "a"])

    def test_a_damaged_history_is_reported_then_replaced_by_a_save_that_keeps_a_copy(self):
        self.history_file.write_bytes(b"{not json")
        self.assertEqual(self.get("/history").json()["file_state"], "damaged")
        self.save("fresh")
        body = self.get("/history").json()
        self.assertEqual((body["file_state"], [e["requirement"] for e in body["entries"]]), ("ok", ["fresh"]))
        self.assertEqual([p.read_bytes() for p in self.base.glob("history.json.damaged-*")], [b"{not json"])

    # ------------------------------------------------------------------ compatibility

    def test_legacy_entries_stay_exactly_as_saved_and_open_through_the_api(self):
        legacy = {"schema_version": 1, "id": "d" * 32, "result": result_to_dict(run_workflow("A legacy plan"))}
        del legacy["schema_version"]
        del legacy["result"]["schema_version"]
        legacy["result"].pop("fallback_reason", None)
        legacy["result"].pop("run_id", None)
        self.history_file.write_text(json.dumps([legacy], indent=2), encoding="utf-8")

        self.save("A new plan")

        self.assertEqual(json.loads(self.history_file.read_text(encoding="utf-8"))[1], legacy)
        body = self.get("/history").json()
        self.assertEqual((body["file_state"], body["problems"]), ("ok", []))
        self.assertEqual([e["requirement"] for e in body["entries"]], ["A new plan", "A legacy plan"])
        snapshot = self.get(f"/history/{'d' * 32}")
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(snapshot.json()["requirement"], "A legacy plan")

    def test_entries_the_api_cannot_read_are_kept_by_a_save_and_still_reported_as_problems(self):
        newer = {"schema_version": 99, "id": "e" * 32, "result": {}}
        self.history_file.write_text(json.dumps([newer], indent=2), encoding="utf-8")
        self.save("new")
        self.assertEqual(json.loads(self.history_file.read_text(encoding="utf-8"))[1], newer)
        body = self.get("/history").json()
        self.assertEqual(len(body["entries"]), 1)
        self.assertEqual(len(body["problems"]), 1)
        self.assertIn("newer AETHER", body["problems"][0])

    def test_the_api_is_unaffected_by_the_unique_temp_files_and_the_lock_file(self):
        self.save("a")
        (self.base / "history.json.99.deadbeef.tmp").write_text("half", encoding="utf-8")  # a crashed writer's leftover
        body = self.get("/history").json()
        self.assertEqual((body["file_state"], len(body["entries"])), ("ok", 1))
        self.assertEqual(self.get("/health").status_code, 200)
        self.assertIn("history.json.lock", os.listdir(self.base))


if __name__ == "__main__":
    unittest.main()
