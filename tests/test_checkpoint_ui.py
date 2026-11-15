"""Web tests for Checkpoint A template fit: reasons, disabled approval and the explicit override.

Uses Streamlit's AppTest (no browser), temporary run and history folders, and blocks the
network. Generated code is never run.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from streamlit.testing.v1 import AppTest
except ImportError:  # Streamlit is optional for the command-line app
    AppTest = None

APP_FILE = str(Path(__file__).resolve().parent.parent / "app.py")
TIMEOUT = 60


def no_network(*args, **kwargs):
    raise OSError("Network access is blocked in tests")


@unittest.skipIf(AppTest is None, "Streamlit is not installed")
class CheckpointFitUITests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runs = Path(self.temp_dir.name) / "runs"
        patch_env = mock.patch.dict(os.environ, {"AETHER_HISTORY_FILE": str(Path(self.temp_dir.name) / "h.json"),
                                                 "AETHER_RUNS_DIR": str(self.runs)})
        patch_env.start()
        os.environ.pop("AETHER_PLANNER", None)
        patch_net = mock.patch("urllib.request.urlopen", side_effect=no_network)
        patch_net.start()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(patch_env.stop)
        self.addCleanup(patch_net.stop)

    def submit(self, requirement):
        app = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
        app.text_area(key="requirement").input(requirement)
        app.button(key="generate").click().run()
        self.assertFalse(app.exception)
        return app

    def text(self, elements):
        return " ".join(e.value for e in elements)

    def written_files(self):
        return list(self.runs.glob("*/workspace/**/*.py"))

    def test_audit_regressions_cannot_be_approved_or_overridden(self):
        for requirement in ["Book a hotel room online with a web page",
                            "An app that calculates monthly tax and saves records in a database with a web dashboard",
                            "A REST API to manage tasks"]:
            with self.subTest(requirement=requirement):
                app = self.submit(requirement)
                self.assertIn("No supported project type", self.text(app.warning))
                self.assertTrue(app.button(key="approve_plan").disabled)
                self.assertNotIn("override_plan", [b.key for b in app.button])
                self.assertIn("Not supported:", self.text(app.markdown))
        self.assertEqual(self.written_files(), [])

    def test_valid_requirement_shows_reasons_and_supported_requirements(self):
        app = self.submit("Build a to-do website where users can log in and save their tasks")
        markdown = self.text(app.markdown)
        self.assertIn("Names the project type: `to-do`", markdown)
        self.assertIn("Supported requirements: Authentication, Database / storage, Web interface", markdown)
        self.assertIn("Not supported: none", markdown)
        self.assertFalse(app.button(key="approve_plan").disabled)
        self.assertNotIn("override_plan", [b.key for b in app.button])

    def test_poor_fit_needs_an_acknowledged_override(self):
        app = self.submit("A calculator web page with user accounts")
        self.assertIn("does not support most of the requested features", self.text(app.warning))
        self.assertTrue(app.button(key="approve_plan").disabled)
        self.assertTrue(app.button(key="override_plan").disabled)  # until the consequences are acknowledged
        self.assertIn("will **not** be built", self.text(app.markdown))
        self.assertEqual(self.written_files(), [])

        app.checkbox[0].check().run()
        self.assertFalse(app.button(key="override_plan").disabled)
        app.button(key="override_plan").click().run()

        self.assertFalse(app.exception)
        self.assertIn("The Developer generated 4 files", app.success[-1].value)
        self.assertIn("explicitly chose the Command-line calculator", self.text(app.warning))
        self.assertEqual(len(self.written_files()), 3)  # calculator.py, tests/__init__.py, tests/test_calculator.py


if __name__ == "__main__":
    unittest.main()
