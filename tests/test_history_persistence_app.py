"""Streamlit regression tests for the hardened history (Task 013): the app keeps working, and a persistence problem
shows the existing warning instead of silently losing history.

Uses Streamlit's AppTest (no browser), temporary history and run folders, and a blocked network."""

import contextlib
import json
import os
import threading
import unittest
from pathlib import Path
from unittest import mock

try:
    from streamlit.testing.v1 import AppTest  # noqa: F401  (the page tests below need it)
except ImportError:  # Streamlit is optional for the command-line app
    AppTest = None

try:  # works under `discover -s tests` and under `python -m unittest tests.test_history_persistence_app`
    from test_history_page import open_buttons, open_page
    from test_workspace_page import CALCULATOR, TODO, WorkspacePageTestCase
except ImportError:
    from tests.test_history_page import open_buttons, open_page
    from tests.test_workspace_page import CALCULATOR, TODO, WorkspacePageTestCase

from aether import fileio
from aether_ui import history

SAVED = "Plan saved to history"
WARNING = "The plan was generated but could not be saved to history."


@unittest.skipIf(AppTest is None, "Streamlit is not installed")
class HistoryPersistenceAppTests(WorkspacePageTestCase):
    def setUp(self):
        super().setUp()
        self.history_file = self.base / "history.json"
        for name, value in (("RETRY_DELAY", 0.0), ("LOCK_POLL", 0.005)):
            patcher = mock.patch.object(fileio, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def toasts(self, app) -> list[str]:
        return [t.value for t in app.toast]

    def requirements(self) -> list[str]:
        return [history.entry_to_result(e).requirement for e in history.load_history(self.history_file)]

    @contextlib.contextmanager
    def history_busy(self):
        """Another operation keeps the history locked, as a second program saving at that moment would."""
        release, held = threading.Event(), threading.Event()

        def holder():
            with fileio.exclusive_lock(history.lock_path(self.history_file)):
                held.set()
                release.wait(30)

        thread = threading.Thread(target=holder)
        thread.start()
        try:
            self.assertTrue(held.wait(5))
            with mock.patch.object(fileio, "LOCK_TIMEOUT", 0.3):
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

        with mock.patch.object(Path, "read_text", blocked):
            yield

    # ------------------------------------------------------------------ normal use is unchanged

    def test_every_generated_plan_is_kept_and_listed_on_the_history_page(self):
        first = self.submit(CALCULATOR)
        second = self.submit(TODO)
        self.assertEqual(self.toasts(first), [SAVED])
        self.assertEqual(self.toasts(second), [SAVED])
        self.assertEqual(self.requirements(), [TODO, CALCULATOR])

        page = open_page("history")
        self.assertFalse(page.exception)
        self.assertEqual(len(open_buttons(page)), 2)

    def test_delete_all_still_works_through_the_locked_atomic_clear(self):
        self.submit(CALCULATOR)
        page = open_page("history")
        self.assertEqual(len(open_buttons(page)), 1)
        page.button(key="confirm_clear").click().run()
        self.assertFalse(page.exception)
        self.assertEqual(open_buttons(page), [])
        self.assertEqual(json.loads(self.history_file.read_text(encoding="utf-8")), [])
        self.assertEqual(sorted(p.name for p in self.base.iterdir() if p.is_file()), ["history.json", "history.json.lock"])

    def test_a_damaged_history_is_copied_aside_and_the_new_plan_is_saved(self):
        self.history_file.write_text("garbage, not json", encoding="utf-8")
        app = self.submit(CALCULATOR)
        self.assertEqual(self.toasts(app), [SAVED])
        self.assertEqual(self.requirements(), [CALCULATOR])
        copies = list(self.base.glob("history.json.damaged-*"))
        self.assertEqual([c.read_text(encoding="utf-8") for c in copies], ["garbage, not json"])

    # ------------------------------------------------------------------ problems are explicit, nothing is lost

    def test_a_busy_history_shows_the_warning_and_loses_nothing(self):
        self.submit(CALCULATOR)
        before = self.history_file.read_bytes()
        with self.history_busy():
            app = self.submit(TODO)
        self.assertEqual(self.toasts(app), [WARNING])
        self.assertTrue(app.session_state["result"].ok, "the plan itself was still generated")
        self.assertFalse(app.exception)
        self.assertEqual(self.history_file.read_bytes(), before)

        later = self.submit(TODO)  # the history works again once the other operation has finished
        self.assertEqual(self.toasts(later), [SAVED])
        self.assertEqual(self.requirements(), [TODO, CALCULATOR])

    def test_an_unreadable_history_is_not_overwritten_when_a_plan_is_generated(self):
        """Before the fix this replaced the whole history with the one new entry."""
        self.submit(CALCULATOR)
        self.submit(TODO)
        before = self.history_file.read_bytes()
        with self.history_unreadable():
            app = self.submit(CALCULATOR)
        self.assertEqual(self.toasts(app), [WARNING])
        self.assertFalse(app.exception)
        self.assertEqual(self.history_file.read_bytes(), before)
        self.assertEqual(list(self.base.glob("history.json.damaged-*")), [])
        self.assertEqual(self.requirements(), [TODO, CALCULATOR])

    def test_a_write_that_fails_keeps_the_history_and_warns(self):
        self.submit(CALCULATOR)
        before = self.history_file.read_bytes()
        real = os.replace

        def refuse_history(source, target):
            if Path(target) == self.history_file:
                raise PermissionError("another program has the history open")
            return real(source, target)  # the run's own files are still saved

        with mock.patch.object(fileio, "REPLACE_ATTEMPTS", 2), mock.patch.object(fileio.os, "replace", refuse_history):
            app = self.submit(TODO)
        self.assertEqual(self.toasts(app), [WARNING])
        self.assertTrue(app.session_state["result"].ok)
        self.assertEqual(self.history_file.read_bytes(), before)
        self.assertEqual([p.name for p in self.base.glob("*.tmp")], [])

    def test_the_run_is_still_usable_when_the_history_is_busy(self):
        with self.history_busy():
            app = self.submit(CALCULATOR)
        self.assertEqual(self.toasts(app), [WARNING])
        self.approve(app)
        self.assertIn("The Developer generated 4 files", self.text(app.success))
        self.assertEqual(len(app.error), 0)


if __name__ == "__main__":
    unittest.main()
