"""History and run persistence hardening (Task 013): no lost updates, atomic writes, explicit failures.

Covers aether/fileio.py (atomic write, retry, cross-thread and cross-process lock), aether_ui/history.py (locked,
strict read-modify-write) and RunStore's atomic saves. Everything uses temporary folders and never touches real
history. Concurrency is tested with real threads AND real child processes; the read-to-write window is widened on
purpose so that a missing lock would lose entries every time, not only occasionally.
"""

import contextlib
import datetime
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from aether import fileio
from aether.context import AgentEvent, ProjectContext, RunStore
from aether_ui import history
from aether_ui.workflow import result_to_dict, run_workflow

ROOT = Path(__file__).resolve().parent.parent
ASIDE = "history.json.damaged-*"

# Run in a child process: save `count` runs to the history file, with the read-to-write window widened.
SAVER = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from aether_ui import history
from aether_ui.workflow import run_workflow
path, worker, count = Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
real = history._read_for_update
def slow(p):
    found = real(p)
    time.sleep(0.03)
    return found
history._read_for_update = slow
for i in range(count):
    history.save_run(run_workflow(f"worker {worker} item {i}"), path)
"""

# Run in a child process: hold the lock, say so, then either wait or die abruptly while holding it.
HOLDER = r"""
import os, sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from aether import fileio
with fileio.exclusive_lock(Path(sys.argv[2])):
    print("READY", flush=True)
    if sys.argv[3] == "die":
        os._exit(0)
    time.sleep(float(sys.argv[3]))
"""


def requirements(path: Path) -> list[str]:
    return [history.entry_to_result(e).requirement for e in history.load_history(path)]


class PersistenceTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.folder = Path(self.temp_dir.name)
        self.path = self.folder / "history.json"
        for name, value in (("RETRY_DELAY", 0.0), ("LOCK_POLL", 0.005)):  # keep the retry tests fast
            patcher = mock.patch.object(fileio, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def save(self, label: str, path: Path | None = None) -> dict:
        return history.save_run(run_workflow(label), path or self.path)

    def widen_the_window(self, seconds: float = 0.03):
        """Sleep between reading the history and writing it, so unlocked writers would overwrite each other."""
        real = history._read_for_update

        def slow(path):
            found = real(path)
            time.sleep(seconds)
            return found

        patcher = mock.patch.object(history, "_read_for_update", slow)
        patcher.start()
        self.addCleanup(patcher.stop)

    def files_in_folder(self) -> list[str]:
        return sorted(p.name for p in self.folder.iterdir())

    @contextlib.contextmanager
    def reading_blocked(self, error: Exception):
        """While active, reading the history file raises `error` (every other file reads normally)."""
        real = Path.read_text

        def blocked(path, *args, **kwargs):
            if Path(path) == self.path:
                raise error
            return real(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", blocked):
            yield


# ====================================================================== the file helpers

class AtomicWriteTests(PersistenceTestCase):
    def test_the_file_is_replaced_and_no_temp_file_is_left(self):
        fileio.write_text_atomic(self.path, "one")
        fileio.write_text_atomic(self.path, "two")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "two")
        self.assertEqual(self.files_in_folder(), ["history.json"])

    def test_every_write_uses_its_own_temp_file(self):
        names = []
        real = os.replace

        def spy(source, target):
            names.append(Path(source).name)
            return real(source, target)

        with mock.patch.object(fileio.os, "replace", spy):
            for _ in range(5):
                fileio.write_text_atomic(self.path, "x")
        self.assertEqual(len(set(names)), 5, names)
        self.assertTrue(all(n.startswith("history.json.") and n.endswith(".tmp") for n in names), names)
        self.assertTrue(all(str(os.getpid()) in n for n in names))

    def test_a_replace_that_keeps_failing_leaves_the_old_file_and_no_temp(self):
        self.path.write_text("old", encoding="utf-8")
        with mock.patch.object(fileio, "REPLACE_ATTEMPTS", 3), \
                mock.patch.object(fileio.os, "replace", side_effect=PermissionError("held open")) as replace:
            with self.assertRaises(PermissionError):
                fileio.write_text_atomic(self.path, "new")
        self.assertEqual(replace.call_count, 3)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "old")
        self.assertEqual(self.files_in_folder(), ["history.json"])

    def test_a_brief_permission_error_on_replace_is_retried(self):
        self.path.write_text("old", encoding="utf-8")
        real = os.replace
        calls = []

        def flaky(source, target):
            calls.append(1)
            if len(calls) <= 3:
                raise PermissionError("another program has the file open")
            return real(source, target)

        with mock.patch.object(fileio.os, "replace", flaky):
            fileio.write_text_atomic(self.path, "new")
        self.assertEqual((len(calls), self.path.read_text(encoding="utf-8")), (4, "new"))
        self.assertEqual(self.files_in_folder(), ["history.json"])

    def test_a_disk_error_while_writing_keeps_the_old_file_and_removes_the_temp(self):
        self.path.write_text("old", encoding="utf-8")
        with mock.patch.object(fileio.os, "fsync", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                fileio.write_text_atomic(self.path, "new content")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "old")
        self.assertEqual(self.files_in_folder(), ["history.json"])

    def test_an_interrupted_write_is_cleaned_up_too(self):
        self.path.write_text("old", encoding="utf-8")
        with mock.patch.object(fileio.os, "replace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                fileio.write_text_atomic(self.path, "new")
        self.assertEqual((self.path.read_text(encoding="utf-8"), self.files_in_folder()), ("old", ["history.json"]))

    def test_the_content_is_flushed_to_disk_before_the_replace(self):
        order = []
        real_fsync, real_replace = os.fsync, os.replace
        with mock.patch.object(fileio.os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1]), \
                mock.patch.object(fileio.os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))[1]):
            fileio.write_text_atomic(self.path, "data")
        self.assertEqual(order, ["fsync", "replace"])

    def test_text_is_written_as_utf_8(self):
        fileio.write_text_atomic(self.path, "naïve – café ✓")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "naïve – café ✓")

    def test_read_text_retry_retries_a_brief_lock_and_then_gives_up(self):
        self.path.write_text("hello", encoding="utf-8")
        real = Path.read_text
        calls = []

        def locked_twice(path, *args, **kwargs):
            if Path(path) == self.path and len(calls) < 2:
                calls.append(1)
                raise PermissionError("locked")
            return real(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", locked_twice):
            self.assertEqual(fileio.read_text_retry(self.path), "hello")
        self.assertEqual(len(calls), 2)
        with self.reading_blocked(PermissionError("locked for good")), self.assertRaises(PermissionError):
            fileio.read_text_retry(self.path)


class ExclusiveLockTests(PersistenceTestCase):
    def setUp(self):
        super().setUp()
        self.lock = self.folder / "thing.lock"

    def test_threads_cannot_both_be_inside_at_once_so_no_update_is_lost(self):
        counter = self.folder / "counter.txt"
        counter.write_text("0", encoding="utf-8")

        def bump():
            with fileio.exclusive_lock(self.lock):
                value = int(counter.read_text(encoding="utf-8"))
                time.sleep(0.01)  # a wide window between read and write
                counter.write_text(str(value + 1), encoding="utf-8")

        threads = [threading.Thread(target=bump) for _ in range(12)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(counter.read_text(encoding="utf-8"), "12")

    def test_a_second_taker_times_out_while_the_lock_is_held_and_succeeds_after(self):
        with fileio.exclusive_lock(self.lock):
            outcome = []

            def other():
                try:
                    with fileio.exclusive_lock(self.lock, timeout=0.2):
                        outcome.append("got it")
                except fileio.LockTimeout as error:
                    outcome.append(error)

            thread = threading.Thread(target=other)
            thread.start()
            thread.join()
        self.assertIsInstance(outcome[0], fileio.LockTimeout)
        self.assertIsInstance(outcome[0], OSError)
        with fileio.exclusive_lock(self.lock, timeout=0.2):  # free again
            pass

    def test_the_lock_is_released_when_the_block_fails(self):
        with self.assertRaises(RuntimeError):
            with fileio.exclusive_lock(self.lock):
                raise RuntimeError("boom")
        with fileio.exclusive_lock(self.lock, timeout=0.2):
            pass

    def test_another_process_is_kept_out(self):
        holder = subprocess.Popen([sys.executable, "-B", "-I", "-c", HOLDER, str(ROOT), str(self.lock), "1.5"],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "READY")
        started = time.monotonic()
        with self.assertRaises(fileio.LockTimeout):
            with fileio.exclusive_lock(self.lock, timeout=0.3):
                self.fail("the lock was held by another process")
        self.assertLess(time.monotonic() - started, 1.2)
        holder.wait(timeout=20)
        with fileio.exclusive_lock(self.lock, timeout=5):  # released when that process ended
            pass

    def test_a_process_that_dies_holding_the_lock_does_not_leave_it_locked(self):
        holder = subprocess.Popen([sys.executable, "-B", "-I", "-c", HOLDER, str(ROOT), str(self.lock), "die"],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "READY")
        holder.wait(timeout=20)
        self.assertTrue(self.lock.exists(), "the lock FILE stays; only the OS lock is released")
        with fileio.exclusive_lock(self.lock, timeout=5):
            pass

    def test_a_lock_file_that_cannot_be_created_is_an_oserror(self):
        blocker = self.folder / "blocker"
        blocker.write_text("x", encoding="utf-8")
        with self.assertRaises(OSError):
            with fileio.exclusive_lock(blocker / "thing.lock"):
                self.fail("not reachable")
        self.assertEqual(blocker.read_text(encoding="utf-8"), "x")


# ====================================================================== concurrent history writers

class ConcurrentWriterTests(PersistenceTestCase):
    def test_threads_do_not_lose_each_others_entries(self):
        self.widen_the_window()
        errors = []

        def worker(n):
            try:
                self.save(f"thread {n}")
            except Exception as error:  # reported below: a failed save would also be a problem
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(12)]
        [t.start() for t in threads]
        [t.join() for t in threads]

        self.assertEqual(errors, [])
        self.assertEqual(sorted(requirements(self.path)), sorted(f"thread {n}" for n in range(12)))
        self.assertEqual(self.files_in_folder(), ["history.json", "history.json.lock"])
        self.assertEqual(history.load_history_report(self.path)[1], [])

    def test_separate_programs_do_not_lose_each_others_entries(self):
        workers = [subprocess.Popen([sys.executable, "-B", "-I", "-c", SAVER, str(ROOT), str(self.path), str(w), "3"],
                                    stderr=subprocess.PIPE, text=True) for w in range(6)]
        for worker in workers:
            _, stderr = worker.communicate(timeout=180)
            self.assertEqual(worker.returncode, 0, stderr)

        found = requirements(self.path)
        self.assertEqual(sorted(found), sorted(f"worker {w} item {i}" for w in range(6) for i in range(3)))
        self.assertEqual(len(found), len(set(found)), "an entry was saved twice")
        self.assertEqual(self.files_in_folder(), ["history.json", "history.json.lock"])

    def test_the_limit_of_25_entries_holds_under_concurrency_and_the_newest_are_kept(self):
        errors = []

        def worker(n):
            try:
                for i in range(3):
                    self.save(f"w{n}-{i}")
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(10)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        found = requirements(self.path)
        self.assertEqual(len(found), history.MAX_ENTRIES)
        self.assertEqual(len(set(found)), history.MAX_ENTRIES)
        self.assertEqual(history.load_history_report(self.path)[1], [])

    def test_clearing_while_saving_always_leaves_a_valid_history(self):
        self.widen_the_window(0.005)
        errors = []

        def savers():
            try:
                for i in range(6):
                    self.save(f"s{i}")
            except Exception as error:
                errors.append(error)

        def clearers():
            try:
                for _ in range(4):
                    history.clear_history(self.path)
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=savers), threading.Thread(target=clearers)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        data = json.loads(self.path.read_text(encoding="utf-8"))  # parses: never half a file
        self.assertIsInstance(data, list)
        self.assertLessEqual(len(data), 6)

    def test_a_reader_never_sees_a_half_written_file(self):
        self.save("first")
        patcher = mock.patch.object(fileio, "RETRY_DELAY", 0.05)  # the real retry pause, not the fast test setting
        patcher.start()
        self.addCleanup(patcher.stop)
        stop = threading.Event()
        problems, reads = [], []

        def reader():
            while not stop.is_set():
                try:
                    data = json.loads(fileio.read_text_retry(self.path))
                    if not isinstance(data, list):
                        problems.append(f"not a list: {type(data)}")
                    reads.append(len(data))
                except Exception as error:  # a partial file would raise JSONDecodeError here
                    problems.append(repr(error))

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for i in range(15):
                self.save(f"entry {i}")
        finally:
            stop.set()
            thread.join()
        self.assertEqual(problems, [])
        self.assertGreater(len(reads), 5)
        self.assertEqual(reads, sorted(reads), "the history only ever grows here, so reads never go backwards")


# ====================================================================== failures are explicit, nothing is lost

class FailureTests(PersistenceTestCase):
    def seed(self, *labels) -> bytes:
        for label in labels:
            self.save(label)
        return self.path.read_bytes()

    def test_the_errors_are_oserrors_so_existing_handlers_keep_working(self):
        for error in (history.HistoryReadError, history.HistoryLockError, history.HistoryWriteError):
            self.assertTrue(issubclass(error, history.HistoryError))
            self.assertTrue(issubclass(error, OSError))

    def test_a_history_that_cannot_be_read_is_not_replaced_by_a_new_one(self):
        """THE data-loss case (R1): a read error used to look like 'damaged', so the whole history was replaced."""
        before = self.seed("a", "b", "c")
        for error in (PermissionError("locked"), OSError(5, "I/O error")):
            with self.subTest(error=type(error).__name__):
                with self.reading_blocked(error), self.assertRaises(history.HistoryReadError) as caught:
                    self.save("new one")
                self.assertIsInstance(caught.exception, OSError)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(sorted(self.files_in_folder()), ["history.json", "history.json.lock"],
                                 "no copy was made aside and no temp file was left")
        self.save("after the problem is gone")  # and saving works again, on top of the untouched history
        self.assertEqual(requirements(self.path), ["after the problem is gone", "c", "b", "a"])

    def test_a_brief_read_lock_is_retried_and_the_entry_is_saved_on_top_of_the_old_ones(self):
        self.seed("a", "b")
        real = Path.read_text
        calls = []

        def locked_once(path, *args, **kwargs):
            if Path(path) == self.path and not calls:
                calls.append(1)
                raise PermissionError("another program has it open")
            return real(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", locked_once):
            self.save("c")
        self.assertEqual(calls, [1])
        self.assertEqual(requirements(self.path), ["c", "b", "a"])

    def test_displaying_an_unreadable_history_stays_lenient_and_changes_nothing(self):
        """Streamlit's behaviour (known issue K36) is unchanged: unreadable shows as empty. It never writes."""
        before = self.seed("a")
        with self.reading_blocked(PermissionError("locked")):
            self.assertEqual(history.load_history(self.path), [])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.files_in_folder(), ["history.json", "history.json.lock"])

    def test_a_write_that_fails_keeps_the_previous_history(self):
        before = self.seed("a", "b")
        with mock.patch.object(fileio, "REPLACE_ATTEMPTS", 2), \
                mock.patch.object(fileio.os, "replace", side_effect=PermissionError("held open")):
            with self.assertRaises(history.HistoryWriteError) as caught:
                self.save("c")
        self.assertIsInstance(caught.exception, OSError)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.files_in_folder(), ["history.json", "history.json.lock"])
        self.save("d")  # and the history still works afterwards
        self.assertEqual(requirements(self.path), ["d", "b", "a"])

    def test_a_full_disk_while_saving_keeps_the_previous_history(self):
        before = self.seed("a")
        with mock.patch.object(fileio.os, "fsync", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(history.HistoryWriteError):
                self.save("b")
        self.assertEqual((self.path.read_bytes(), self.files_in_folder()), (before, ["history.json", "history.json.lock"]))

    def test_clearing_that_fails_keeps_the_history(self):
        before = self.seed("a")
        with mock.patch.object(fileio, "REPLACE_ATTEMPTS", 1), \
                mock.patch.object(fileio.os, "replace", side_effect=PermissionError("held open")):
            with self.assertRaises(history.HistoryWriteError):
                history.clear_history(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_a_busy_history_raises_instead_of_waiting_forever_and_nothing_is_changed(self):
        before = self.seed("a")
        release, held = threading.Event(), threading.Event()

        def holder():
            with fileio.exclusive_lock(history.lock_path(self.path)):
                held.set()
                release.wait(20)

        thread = threading.Thread(target=holder)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(release.set)
        held.wait(5)
        with mock.patch.object(fileio, "LOCK_TIMEOUT", 0.2):
            with self.assertRaises(history.HistoryLockError) as caught:
                self.save("b")
            with self.assertRaises(history.HistoryLockError):
                history.clear_history(self.path)
        self.assertIsInstance(caught.exception, OSError)
        self.assertEqual(self.path.read_bytes(), before)
        release.set()
        thread.join()
        self.save("c")  # works once the other operation is finished
        self.assertEqual(requirements(self.path), ["c", "a"])

    def test_a_folder_that_cannot_be_prepared_is_an_explicit_write_error(self):
        blocker = self.folder / "blocker"
        blocker.write_text("x", encoding="utf-8")
        with self.assertRaises(history.HistoryWriteError) as caught:
            self.save("a", blocker / "history.json")
        self.assertIsInstance(caught.exception, OSError)
        self.assertEqual(blocker.read_text(encoding="utf-8"), "x")

    def test_error_messages_never_contain_a_path(self):
        self.seed("a")
        with self.reading_blocked(PermissionError(13, "denied", str(self.path))), \
                self.assertRaises(history.HistoryError) as read_error:
            self.save("b")
        with mock.patch.object(fileio, "REPLACE_ATTEMPTS", 1), \
                mock.patch.object(fileio.os, "replace", side_effect=PermissionError(13, "denied", str(self.path))), \
                self.assertRaises(history.HistoryError) as write_error:
            self.save("c")
        for error in (read_error.exception, write_error.exception):
            self.assertNotIn(self.temp_dir.name, str(error))
            self.assertNotIn("history.json", str(error))


class CorruptFileTests(PersistenceTestCase):
    """A file that was READ and is really corrupt keeps the old policy: copy it aside, then start a new history."""

    def assert_replaced_with_one_entry_and_a_copy(self, corrupt: bytes):
        self.path.write_bytes(corrupt)
        self.assertEqual(history.load_history(self.path), [])  # displays as empty, and does not crash
        self.save("fresh")
        copies = list(self.folder.glob(ASIDE))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), corrupt, "the damaged content is kept exactly")
        self.assertEqual(requirements(self.path), ["fresh"])

    def test_not_json(self):
        self.assert_replaced_with_one_entry_and_a_copy(b"this is not json")

    def test_json_that_is_not_a_list(self):
        self.assert_replaced_with_one_entry_and_a_copy(b'{"entries": []}')

    def test_an_empty_file(self):
        self.assert_replaced_with_one_entry_and_a_copy(b"")

    def test_a_truncated_file(self):
        self.save("a")
        self.assert_replaced_with_one_entry_and_a_copy(self.path.read_bytes()[:40])

    def test_invalid_utf8_no_longer_crashes_and_is_treated_as_damaged(self):
        self.assert_replaced_with_one_entry_and_a_copy(b"\xff\xfe\x00[]")

    def test_two_damage_events_in_the_same_second_keep_both_copies(self):
        fixed = mock.Mock()
        fixed.now.return_value = datetime.datetime(2026, 1, 2, 3, 4, 5)
        with mock.patch.object(history, "datetime", fixed):
            self.path.write_bytes(b"first damage")
            self.save("a")
            self.path.write_bytes(b"second damage")
            self.save("b")
        self.assertEqual(sorted(p.read_bytes() for p in self.folder.glob(ASIDE)), [b"first damage", b"second damage"])

    def test_a_damaged_file_that_cannot_be_copied_aside_is_not_replaced(self):
        self.path.write_bytes(b"not json")
        with mock.patch.object(history.shutil, "copy2", side_effect=OSError("disk full")):
            with self.assertRaises(history.HistoryWriteError):
                self.save("a")
        self.assertEqual(self.path.read_bytes(), b"not json")
        self.assertEqual(list(self.folder.glob(ASIDE)), [])

    def test_a_good_history_is_never_copied_aside(self):
        self.save("a")
        self.save("b")
        self.assertEqual(list(self.folder.glob(ASIDE)), [])

    def test_stale_temp_files_from_a_crashed_writer_are_removed_and_nothing_else_is(self):
        self.save("a")
        (self.folder / "history.json.4242.deadbeef.tmp").write_text("half", encoding="utf-8")
        for keep in ("history.tmp", "other.json.1.abc.tmp", "notes.txt"):
            (self.folder / keep).write_text("keep", encoding="utf-8")
        self.save("b")
        self.assertEqual(self.files_in_folder(),
                         ["history.json", "history.json.lock", "history.tmp", "notes.txt", "other.json.1.abc.tmp"])


# ====================================================================== compatibility

def legacy_entry(requirement="A website with login") -> dict:
    """A history entry saved before schema versioning existed: no schema_version anywhere."""
    entry = {"schema_version": 1, "id": "a" * 32, "result": result_to_dict(run_workflow(requirement))}
    del entry["schema_version"]
    del entry["result"]["schema_version"]
    for field in ("fallback_reason", "run_id"):
        entry["result"].pop(field, None)
    return entry


class CompatibilityTests(PersistenceTestCase):
    def test_legacy_newer_and_junk_entries_survive_concurrent_saves_exactly(self):
        legacy = legacy_entry()
        newer = {**legacy_entry("Another"), "schema_version": 99, "id": "b" * 32}
        junk = "just text"
        original = [newer, junk, legacy]
        self.path.write_text(json.dumps(original, indent=2), encoding="utf-8")
        self.widen_the_window(0.01)
        threads = [threading.Thread(target=self.save, args=(f"new {n}",)) for n in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]

        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved[-3:], original, "the old entries are untouched, not rewritten or stamped")
        self.assertEqual(len(saved), 9)
        entries, problems = history.load_history_report(self.path)
        self.assertEqual(len(entries), 7)  # 6 new + the legacy one
        self.assertEqual(len(problems), 2)  # the newer-schema entry and the junk string
        self.assertIn("newer AETHER", problems[0])

    def test_the_file_format_is_unchanged(self):
        entry = self.save("A website")
        text = self.path.read_text(encoding="utf-8")
        self.assertEqual(json.loads(text), [entry])
        self.assertEqual(text, json.dumps([entry], indent=2))
        self.assertEqual(set(entry), {"schema_version", "id", "result"})
        self.assertEqual(entry["schema_version"], 1)

    def test_a_history_file_written_by_the_previous_version_still_loads_and_is_extended(self):
        old_style = [{"schema_version": 1, "id": "c" * 32, "result": result_to_dict(run_workflow("Old one"))}]
        self.path.write_text(json.dumps(old_style, indent=2), encoding="utf-8")  # as the old _write did
        self.save("New one")
        self.assertEqual(requirements(self.path), ["New one", "Old one"])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))[1], old_style[0])

    def test_reading_creates_no_lock_file_and_no_folder(self):
        folder = self.folder / "not-created-yet"
        self.assertEqual(history.load_history(folder / "history.json"), [])
        self.assertEqual(history.load_history_report(folder / "history.json"), ([], []))
        self.assertFalse(folder.exists())

    def test_the_default_location_and_environment_variable_are_unchanged(self):
        self.assertEqual(history.DEFAULT_PATH.name, "history.json")
        self.assertEqual(history.DEFAULT_PATH.parent.name, ".aether")
        with mock.patch.dict(os.environ, {"AETHER_HISTORY_FILE": str(self.path)}):
            self.assertEqual(history.history_path(), self.path)
            history.save_run(run_workflow("via the environment"))
        self.assertEqual(requirements(self.path), ["via the environment"])

    def test_the_lock_file_sits_next_to_the_history_file(self):
        self.save("a")
        self.assertEqual(history.lock_path(self.path), self.folder / "history.json.lock")
        self.assertEqual(self.path.with_name("history.json.lock").read_bytes(), b"")


class RunStoreAtomicSaveTests(PersistenceTestCase):
    def setUp(self):
        super().setUp()
        self.store = RunStore(self.folder / "runs")

    def context(self, note="n") -> ProjectContext:
        context = ProjectContext.new("A website")
        context.events.append(AgentEvent("t", "Manager", "x", "done", note))
        return context

    def test_a_save_leaves_no_temp_file(self):
        folder = self.store.save(self.context())
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["context.json", "events.jsonl"])

    def test_a_brief_permission_error_is_retried(self):
        context = self.context()
        real = os.replace
        calls = []

        def flaky(source, target):
            calls.append(1)
            if len(calls) <= 2:
                raise PermissionError("another program has the file open")
            return real(source, target)

        with mock.patch.object(fileio.os, "replace", flaky):
            self.store.save(context)
        self.assertEqual(self.store.load(context.run_id), context)

    def test_a_failed_save_keeps_the_previous_saved_run(self):
        context = self.context("before")
        self.store.save(context)
        saved = (self.store.run_dir(context.run_id) / "context.json").read_bytes()
        context.record("Planner", "later", "done", "after")
        with mock.patch.object(fileio, "REPLACE_ATTEMPTS", 1), \
                mock.patch.object(fileio.os, "replace", side_effect=PermissionError("held open")):
            with self.assertRaises(PermissionError):
                self.store.save(context)
        folder = self.store.run_dir(context.run_id)
        self.assertEqual((folder / "context.json").read_bytes(), saved)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["context.json", "events.jsonl"])

    def test_simultaneous_saves_of_one_run_never_corrupt_it(self):
        context = self.context()
        self.store.save(context)
        errors = []
        # Two threads replacing the same file can briefly collide on Windows: give the retry real pauses here.
        patches = (mock.patch.object(fileio, "RETRY_DELAY", 0.01), mock.patch.object(fileio, "REPLACE_ATTEMPTS", 200))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        def save_many():
            try:
                for _ in range(10):
                    self.store.save(context)
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=save_many) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(self.store.load(context.run_id), context)
        folder = self.store.run_dir(context.run_id)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["context.json", "events.jsonl"])


if __name__ == "__main__":
    unittest.main()
