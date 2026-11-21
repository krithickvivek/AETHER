"""Web tests for the app's configuration and persistence error paths (Task 010).

Covers app.py run_store() (an invalid runs folder), the history-save OSError toast, and render_files() when
the runs folder is invalid. Uses Streamlit's AppTest (no browser), temporary history and run folders, and a
blocked network. Generated code is never run and no real user data is read or written.

SAFETY OF THE INVALID-PATH TESTS
    RunStore refuses any folder inside AETHER's own folder. The tests use INVALID_RUNS_FOLDER, a path inside
    that folder, only as a VALUE for AETHER_RUNS_DIR; RunStore merely resolves it and never creates it. Every
    test asserts the folder does not exist before and after, so a bug that created it would fail the test
    (and nothing is deleted by the tests). Environment variables are changed only with mock.patch.dict and
    restored by cleanup; setUpClass/tearDownClass verify they are back to their original values.

Pinned current behaviour (known issue K39, not changed here): with an invalid runs folder the "Generated
projects cannot be saved" error is shown only in the run in which Generate was clicked; on the next rerun it
is gone, and the plan stays on screen with no way to approve it and no explanation. If this is improved,
update test_the_error_disappears_on_the_next_rerun on purpose.

Test 7 (render_files with no store) simulates the runs folder becoming invalid between reruns. That cannot
happen in normal use (a run only exists when the folder was valid), so it documents a defensive branch.
"""

import os
import unittest
from pathlib import Path
from unittest import mock

try:
    from streamlit.testing.v1 import AppTest
except ImportError:  # Streamlit is optional for the command-line app
    AppTest = None

try:  # works under `discover -s tests` and under `python -m unittest tests.test_app_error_paths`
    from test_history_page import open_page
    from test_workspace_page import APP_FILE, CALCULATOR, TIMEOUT, WorkspacePageTestCase
except ImportError:
    from tests.test_history_page import open_page
    from tests.test_workspace_page import APP_FILE, CALCULATOR, TIMEOUT, WorkspacePageTestCase

from aether.context import AWAITING_APPROVAL, COMPLETED, SOURCE_ROOT
from aether_ui import history

INVALID_RUNS_FOLDER = SOURCE_ROOT / "__invalid_runs_folder_for_tests__"  # inside AETHER's folder; never created
CONFIG_ERROR_START = "Generated projects cannot be saved: Runs cannot be stored inside AETHER's own folder"
HISTORY_WARNING = "The plan was generated but could not be saved to history."
ENV_KEYS = ("AETHER_HISTORY_FILE", "AETHER_RUNS_DIR", "AETHER_PLANNER")


@unittest.skipIf(AppTest is None, "Streamlit is not installed")
class ErrorPathTestCase(WorkspacePageTestCase):
    @classmethod
    def setUpClass(cls):
        cls.env_before = {key: os.environ.get(key) for key in ENV_KEYS}

    @classmethod
    def tearDownClass(cls):
        after = {key: os.environ.get(key) for key in ENV_KEYS}
        if after != cls.env_before:
            raise AssertionError(f"Environment variables were not restored: {cls.env_before} -> {after}")

    def setUp(self):
        super().setUp()
        self.assertFalse(INVALID_RUNS_FOLDER.exists(), f"{INVALID_RUNS_FOLDER} must not exist before the test")
        self.addCleanup(self.assert_invalid_folder_was_not_created)
        self.history_file = self.base / "history.json"

    def assert_invalid_folder_was_not_created(self):
        self.assertFalse(INVALID_RUNS_FOLDER.exists(), f"a test created {INVALID_RUNS_FOLDER} inside AETHER's folder")

    def use_invalid_runs_folder(self):
        """From now on (until cleanup) AETHER_RUNS_DIR points inside AETHER's own folder, which RunStore refuses."""
        patcher = mock.patch.dict(os.environ, {"AETHER_RUNS_DIR": str(INVALID_RUNS_FOLDER)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def block_history(self) -> Path:
        """Make saving history fail with a real OSError: the history file's parent folder is a regular file."""
        blocker = self.base / "blocker"
        blocker.write_text("x", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"AETHER_HISTORY_FILE": str(blocker / "history.json")})
        patcher.start()
        self.addCleanup(patcher.stop)
        return blocker

    def toasts(self, app) -> list[str]:
        return [t.value for t in app.toast]

    def explanation_on_page(self, app) -> str:
        """All the text the page shows outside the error elements."""
        return " ".join(self.text(group) for group in (app.markdown, app.warning, app.info, app.success, app.caption))


class InvalidRunsFolderTests(ErrorPathTestCase):
    """app.py 111-113: run_store() when the runs folder is inside AETHER's own folder."""

    def test_the_page_loads_without_an_error_until_the_folder_is_needed(self):
        self.use_invalid_runs_folder()
        app = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()

        self.assertFalse(app.exception)
        self.assertEqual(len(app.error), 0)
        self.assertEqual(self.button_keys(app), ["generate"])

    def test_generate_reports_the_error_and_offers_no_approval(self):
        self.use_invalid_runs_folder()
        app = self.submit(CALCULATOR)

        errors = [e.value for e in app.error]
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].startswith(CONFIG_ERROR_START), errors[0])
        self.assertTrue(app.session_state["result"].ok)  # the plan itself was generated
        self.assertIsNone(app.session_state["context"])  # but there is no run to approve
        keys = self.button_keys(app)
        self.assertNotIn("approve_plan", keys)
        self.assertNotIn("reject_plan", keys)
        entries = history.load_history(self.history_file)  # the plan is still saved to history, without a run id
        self.assertEqual(len(entries), 1)
        self.assertIsNone(history.entry_to_result(entries[0]).run_id)
        self.assertFalse(self.runs.exists(), "nothing may be written to the temporary runs folder either")

    def test_the_error_disappears_on_the_next_rerun(self):
        """PINNED (known issue K39): the error is shown once; afterwards the plan stays, with no way to
        approve it and no explanation."""
        self.use_invalid_runs_folder()
        app = self.submit(CALCULATOR)
        self.assertEqual(len(app.error), 1)

        app.run()  # any later rerun

        self.assertFalse(app.exception)
        self.assertEqual(len(app.error), 0)
        self.assertNotIn("cannot be saved", self.explanation_on_page(app))
        self.assertTrue(app.session_state["result"].ok)  # the plan is still on screen ...
        self.assertIn("Plan generated by RuleBasedPlanner", self.text(app.success))
        self.assertIsNone(app.session_state["context"])  # ... with nothing to approve
        self.assertNotIn("approve_plan", self.button_keys(app))


class HistorySaveErrorTests(ErrorPathTestCase):
    """app.py 133-134: history.save_run() raising OSError while a plan is generated."""

    def test_an_oserror_shows_the_warning_toast_and_keeps_the_plan(self):
        blocker = self.block_history()
        app = self.submit(CALCULATOR)

        self.assertEqual(self.toasts(app), [HISTORY_WARNING])  # and no "Plan saved to history" toast
        self.assertTrue(app.session_state["result"].ok)
        self.assertEqual(len(app.error), 0)
        self.assertEqual(blocker.read_text(encoding="utf-8"), "x")  # the blocker file was not touched
        self.assertFalse((blocker / "history.json").exists())

    def test_the_run_is_still_usable_after_the_history_failure(self):
        self.block_history()
        app = self.submit(CALCULATOR)
        self.assertEqual(app.session_state["context"].status, AWAITING_APPROVAL)
        self.assertFalse(app.button(key="approve_plan").disabled)

        self.approve(app)

        self.assertEqual(app.session_state["context"].status, COMPLETED)
        self.assertIn("The Developer generated 4 files", self.text(app.success))
        self.assertEqual(len(app.error), 0)

    def test_control_a_normal_save_shows_the_saved_toast(self):
        """Proves the toast assertions above are meaningful: a working history gives the other toast."""
        app = self.submit(CALCULATOR)

        self.assertEqual(self.toasts(app), ["Plan saved to history"])
        self.assertEqual(len(history.load_history(self.history_file)), 1)


class RunsFolderBecomesInvalidTests(ErrorPathTestCase):
    """app.py 382 (render_files with no store) and the related disabled buttons, when the runs folder becomes
    invalid after a run is already on screen (simulated: see the module docstring)."""

    def test_a_completed_run_loses_its_file_list_and_shows_the_error(self):
        app = self.submit(CALCULATOR)
        self.approve(app)
        self.assertTrue(self.file_selector_present(app))
        files_before = sorted(p.name for p in self.runs.rglob("*") if p.is_file())

        self.use_invalid_runs_folder()
        app.run()

        self.assertFalse(app.exception)
        errors = [e.value for e in app.error]
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].startswith(CONFIG_ERROR_START), errors[0])
        self.assertFalse(self.file_selector_present(app))  # render_files returned early
        self.assertEqual(app.session_state["context"].status, COMPLETED)  # the run itself is untouched
        self.assertEqual(sorted(p.name for p in self.runs.rglob("*") if p.is_file()), files_before)

    def test_a_run_at_the_checkpoint_cannot_be_approved_or_rejected(self):
        app = self.submit(CALCULATOR)
        self.assertFalse(app.button(key="approve_plan").disabled)

        self.use_invalid_runs_folder()
        app.run()

        self.assertFalse(app.exception)
        self.assertEqual(len(app.error), 1)
        self.assertTrue(app.button(key="approve_plan").disabled)
        self.assertTrue(app.button(key="reject_plan").disabled)
        self.assertEqual(app.session_state["context"].status, AWAITING_APPROVAL)
        self.assertEqual(self.written_files(), [])

    def test_opening_a_saved_run_from_history_with_an_invalid_folder_still_works(self):
        """load_context() catches the ValueError from RunStore(): the run opens from its saved result only."""
        app = self.submit(CALCULATOR)
        self.approve(app)
        history_before = self.history_file.read_bytes()

        self.use_invalid_runs_folder()
        page = open_page("history")
        self.assertFalse(page.exception)
        buttons = [b for b in page.button if (b.key or "").startswith("open-")]
        self.assertEqual(len(buttons), 1)
        buttons[0].click().run()

        self.assertFalse(page.exception)
        self.assertEqual(page.session_state["result_source"], "history")
        self.assertIsNone(page.session_state["context"])
        self.assertIn("Showing a saved run", self.text(page.info))
        self.assertFalse(self.file_selector_present(page))
        self.assertEqual(self.history_file.read_bytes(), history_before)


if __name__ == "__main__":
    unittest.main()
