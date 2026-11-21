"""Web tests for the Workspace page: the Developer failure message and the template-fit warnings (Task 009).

Groups B and C. Uses Streamlit's AppTest (no browser), temporary history and run folders, and a blocked
network. Write failures use the Workspace's real limits (MAX_FILE_BYTES, MAX_FILES). Generated code is never
run, and no real user data is read or written.

Reuses the Task 008 helpers `html_text` (reads st.html content; the status badges are st.html) and `open_page`
(opens the History page). Those helpers are where the Streamlit internals live; this file adds none.

Pinned current behaviour (known issue K38, not changed here): if the Developer fails AFTER writing some files,
the Workspace page shows the partial file list and NO error message; the failure is only visible in the
progress strip and the activity log. Test_partial_write_failure_shows_files_but_no_error_message pins that.
If the message is added later, update that test on purpose.
"""

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from streamlit.testing.v1 import AppTest
except ImportError:  # Streamlit is optional for the command-line app
    AppTest = None

try:  # works under `discover -s tests` and under `python -m unittest tests.test_workspace_page`
    from test_history_page import html_text, open_page
except ImportError:
    from tests.test_history_page import html_text, open_page

from aether import workspace
from aether.context import FAILED
from aether.developer import preview

APP_FILE = str(Path(__file__).resolve().parent.parent / "app.py")
TIMEOUT = 60
CALCULATOR = "A command-line calculator for basic arithmetic"
TODO = "Build a to-do website where users can log in and save their tasks"
TODO_WITH_API = "A to-do website with login and a public API"  # to-do template fits, API is not supported
AMBIGUOUS = "A to-do list for library staff"  # names two project types
FAILURE_LEAD = "The Developer could not generate this project."


def no_network(*args, **kwargs):
    raise OSError("Network access is blocked in tests")


@unittest.skipIf(AppTest is None, "Streamlit is not installed")
class WorkspacePageTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base = Path(self.temp_dir.name)
        self.runs = self.base / "runs"
        patch_env = mock.patch.dict(os.environ, {"AETHER_HISTORY_FILE": str(self.base / "history.json"),
                                                 "AETHER_RUNS_DIR": str(self.runs)})
        patch_env.start()
        os.environ.pop("AETHER_PLANNER", None)
        patch_net = mock.patch("urllib.request.urlopen", side_effect=no_network)
        patch_net.start()
        self.addCleanup(patch_env.stop)
        self.addCleanup(patch_net.stop)

    # ------------------------------------------------------------------ helpers

    def submit(self, requirement: str):
        app = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
        app.text_area(key="requirement").input(requirement)
        app.button(key="generate").click().run()
        self.assertFalse(app.exception)
        return app

    def approve(self, app):
        app.button(key="approve_plan").click().run()
        self.assertFalse(app.exception)

    def text(self, elements) -> str:
        return " ".join(e.value for e in elements)

    def badges(self, app) -> list[str]:
        """The status badges of the checkpoint panel, e.g. 'Partial fit' (rendered with st.html)."""
        return re.findall(r'ae-status [a-z]+">([^<]+)<', html_text(app))

    def written_files(self) -> list[Path]:
        return list(self.runs.glob("*/workspace/**/*.py"))

    def button_keys(self, app) -> list[str]:
        return [b.key for b in app.button]

    def file_selector_present(self, app) -> bool:
        return "file_choice" in [s.key for s in app.selectbox]


class DeveloperFailureMessageTests(WorkspacePageTestCase):
    """Group B: what the Workspace page shows when the Developer cannot write the files."""

    def test_first_file_failure_shows_the_error_and_no_files(self):
        app = self.submit(CALCULATOR)
        with mock.patch.object(workspace, "MAX_FILE_BYTES", 10):
            self.approve(app)

        context = app.session_state["context"]
        self.assertEqual(context.status, FAILED)
        self.assertEqual(context.files, [])
        errors = [e.value for e in app.error]
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].startswith(f"{FAILURE_LEAD} Could not write project files:"), errors[0])
        self.assertIn("(limit 10)", errors[0])
        self.assertFalse(self.file_selector_present(app))
        keys = self.button_keys(app)
        self.assertNotIn("approve_plan", keys)  # a failed run cannot be approved again
        self.assertNotIn("reject_plan", keys)
        self.assertEqual(self.written_files(), [])

    def test_partial_write_failure_shows_files_but_no_error_message(self):
        """PINNED (known issue K38): files were written before the failure, so the page lists them but never
        says that the Developer failed. Update this test when the message is added."""
        app = self.submit(CALCULATOR)
        with mock.patch.object(workspace, "MAX_FILES", 1):  # the second file is refused
            self.approve(app)

        context = app.session_state["context"]
        self.assertEqual(context.status, FAILED)
        self.assertEqual([f.path for f in context.files], ["calculator.py"])
        self.assertTrue(any(e.agent == "Developer" and e.status == FAILED and "maximum of 1 files" in e.detail
                            for e in context.events), "the failure is recorded in the run's activity log")
        # The page shows the partial project ...
        self.assertTrue(self.file_selector_present(app))
        self.assertEqual(len(app.selectbox(key="file_choice").options), 1)
        # ... but no failure message anywhere (K38).
        self.assertEqual(len(app.error), 0)
        self.assertNotIn(FAILURE_LEAD, self.text(app.markdown) + self.text(app.warning) + self.text(app.info)
                         + self.text(app.success) + html_text(app))
        self.assertNotIn("approve_plan", self.button_keys(app))
        self.assertEqual(len(self.written_files()), 1)

    def test_a_failed_run_reopened_from_history_shows_the_same_error(self):
        app = self.submit(CALCULATOR)
        with mock.patch.object(workspace, "MAX_FILE_BYTES", 10):
            self.approve(app)
        self.assertEqual(len(app.error), 1)

        history_page = open_page("history")
        self.assertFalse(history_page.exception)
        buttons = [b for b in history_page.button if (b.key or "").startswith("open-")]
        self.assertEqual(len(buttons), 1)
        buttons[0].click().run()

        self.assertFalse(history_page.exception)
        self.assertEqual(history_page.session_state["context"].status, FAILED)
        errors = [e.value for e in history_page.error]
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].startswith(f"{FAILURE_LEAD} Could not write project files:"), errors[0])
        self.assertIn("Showing a saved run", self.text(history_page.info))


class WorkspaceWarningTests(WorkspacePageTestCase):
    """Group C: the template-fit warnings and badges shown at Checkpoint A."""

    def test_partial_fit_warns_about_unsupported_features_and_uncovered_steps(self):
        app = self.submit(TODO_WITH_API)
        plan_preview = preview(app.session_state["context"])
        self.assertEqual(plan_preview.choice.unsupported, ["api"])
        self.assertTrue(plan_preview.uncovered_steps, "this requirement should leave some plan steps without a file")

        warnings = [w.value for w in app.warning]
        self.assertIn("This template does not include: API. Those parts of the plan will not be built.", warnings)
        steps = ", ".join(map(str, plan_preview.uncovered_steps))
        self.assertIn(f"No generated file carries out plan step(s) {steps}.", warnings)
        badges = self.badges(app)
        self.assertIn("Partial fit", badges)
        self.assertIn(f"{len(plan_preview.files)} files planned", badges)
        self.assertFalse(app.button(key="approve_plan").disabled)  # a partial fit can still be approved
        self.assertEqual(self.written_files(), [])  # nothing is written before approval

    def test_a_fully_fitting_requirement_shows_neither_warning(self):
        app = self.submit(TODO)
        plan_preview = preview(app.session_state["context"])
        self.assertEqual(plan_preview.choice.unsupported, [])
        self.assertEqual(plan_preview.uncovered_steps, [])

        warnings = self.text(app.warning)
        self.assertNotIn("This template does not include", warnings)
        self.assertNotIn("No generated file carries out", warnings)
        self.assertIn("Fits the requirement", self.badges(app))
        self.assertFalse(app.button(key="approve_plan").disabled)

    def test_ambiguous_requirement_shows_the_ambiguous_badge_and_cannot_be_approved(self):
        app = self.submit(AMBIGUOUS)

        self.assertIn("Ambiguous: more than one project type", self.badges(app))
        self.assertIn("No files planned", self.badges(app))
        warnings = self.text(app.warning)
        self.assertIn("names more than one project type", warnings)
        self.assertIn("To-do web app", warnings)
        self.assertIn("Library management REST API", warnings)
        self.assertTrue(app.button(key="approve_plan").disabled)
        self.assertTrue(app.button(key="override_plan").disabled)  # until the consequences are acknowledged
        self.assertFalse(app.button(key="reject_plan").disabled)
        self.assertEqual(self.written_files(), [])


if __name__ == "__main__":
    unittest.main()
