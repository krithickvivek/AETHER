"""Tests for the Streamlit web interface, run without a browser.

Uses Streamlit's built-in AppTest. Skipped if Streamlit is not installed,
so the core tests still run with the standard library alone.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from streamlit.testing.v1 import AppTest
except ImportError:
    AppTest = None

from aether_ui import history
from test_ollama_planner import GOOD_STEPS, chat_response, no_network

APP_FILE = str(Path(__file__).resolve().parent.parent / "app.py")
TIMEOUT = 20  # seconds; AppTest's 3-second default is too tight for a cold first load


class FakeOllamaServer:
    """Replaces urllib.request.urlopen with canned Ollama answers. No real network calls."""

    def __init__(self, chat_answer: dict):
        self.chat_answer = chat_answer

    def __call__(self, request, timeout=None):
        url = getattr(request, "full_url", request)
        if url.endswith("/api/tags"):
            body = {"models": [{"name": "llama3.2:1b"}]}
        elif url.endswith("/api/chat"):
            body = self.chat_answer
        else:
            raise AssertionError(f"Unexpected URL {url}")
        return FakeResponse(json.dumps(body).encode("utf-8"))


class FakeResponse:
    def __init__(self, data: bytes):
        self.data = data

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@unittest.skipIf(AppTest is None, "Streamlit is not installed")
class WebAppTests(unittest.TestCase):
    def setUp(self):
        # Keep test runs out of the real history file, and block real network calls.
        self.temp_dir = tempfile.TemporaryDirectory()
        self.history_file = Path(self.temp_dir.name) / "history.json"
        os.environ["AETHER_HISTORY_FILE"] = str(self.history_file)
        os.environ["AETHER_RUNS_DIR"] = str(Path(self.temp_dir.name) / "runs")
        os.environ.pop("AETHER_PLANNER", None)
        self.urlopen = mock.patch("urllib.request.urlopen", side_effect=no_network)
        self.urlopen.start()

    def tearDown(self):
        self.urlopen.stop()
        os.environ.pop("AETHER_HISTORY_FILE", None)
        os.environ.pop("AETHER_RUNS_DIR", None)
        self.temp_dir.cleanup()

    def submit(self, requirement: str, planner: str = "rule"):
        app = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
        if planner != "rule":
            app.radio(key="planner_choice").set_value(planner).run()
        app.text_area(key="requirement").input(requirement)
        app.button(key="generate").click().run()
        return app

    def use_fake_ollama(self, chat_answer: dict):
        self.urlopen.stop()
        self.urlopen = mock.patch("urllib.request.urlopen", side_effect=FakeOllamaServer(chat_answer))
        self.urlopen.start()

    def test_rule_based_planner_is_selected_by_default(self):
        app = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
        self.assertEqual(app.radio(key="planner_choice").value, "rule")

    def test_ai_plan_is_labelled_with_caution(self):
        self.use_fake_ollama(chat_response(GOOD_STEPS))
        app = self.submit("Build a to-do website where users can log in and save tasks", planner="ollama")

        self.assertFalse(app.exception)
        self.assertIn("OllamaPlanner", app.success[0].value)
        self.assertIn("local AI model", app.success[0].value)
        warnings = " ".join(w.value for w in app.warning)
        self.assertIn("can be incomplete or wrong", warnings)
        self.assertNotIn("did not produce a usable plan", warnings)
        saved = history.entry_to_result(history.load_history(self.history_file)[0])
        self.assertEqual(saved.plan.planner_name, "OllamaPlanner")

    def test_bad_ai_output_falls_back_and_says_why(self):
        self.use_fake_ollama({"message": {"content": "not json"}})
        app = self.submit("Build a to-do website where users can log in and save tasks", planner="ollama")

        self.assertFalse(app.exception)
        self.assertIn("RuleBasedPlanner", app.success[0].value)
        warnings = " ".join(w.value for w in app.warning)
        self.assertIn("did not produce a usable plan", warnings)
        self.assertIn("expected JSON", warnings)
        self.assertNotIn("can be incomplete or wrong", warnings)

    def test_workspace_loads_with_empty_state(self):
        app = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
        self.assertFalse(app.exception)
        self.assertIsNone(app.session_state["result"] if "result" in app.session_state else None)
        self.assertEqual(len(app.metric), 0)

    def test_generates_plan_and_shows_handoff(self):
        app = self.submit("Build a to-do website where users can log in and save tasks")

        self.assertFalse(app.exception)
        self.assertFalse(app.error)
        metrics = {metric.label: metric.value for metric in app.metric}
        self.assertEqual(metrics["Features detected"], "3")
        self.assertEqual(metrics["Plan steps"], "18")
        self.assertEqual(metrics["Phases"], "6")
        code_blocks = "\n".join(block.value for block in app.code)
        self.assertIn("TaskBrief(", code_blocks)
        self.assertIn("Handing off to RuleBasedPlanner", code_blocks)

    def test_successful_run_is_saved_to_history(self):
        self.submit("A REST API with a database")
        entries = history.load_history(self.history_file)
        self.assertEqual(len(entries), 1)

    def test_checkpoint_shows_project_type_and_planned_files_before_generation(self):
        app = self.submit("Build a to-do website where users can log in and save tasks")

        self.assertFalse(app.exception)
        self.assertIn("To-do web app", " ".join(m.value for m in app.markdown))
        planned = app.dataframe[0].value["File"].tolist()
        self.assertIn("todo_app/auth.py", planned)
        self.assertFalse(app.button(key="approve_plan").disabled)
        self.assertEqual(list((Path(self.temp_dir.name) / "runs").glob("*/workspace")), [])

    def test_approving_generates_files_and_shows_them(self):
        app = self.submit("A command-line calculator for basic arithmetic")
        app.button(key="approve_plan").click().run()

        self.assertFalse(app.exception)
        self.assertIn("The Developer generated 4 files", app.success[-1].value)
        shown = [label.split("  ·  ")[0] for label in app.selectbox(key="file_choice").options]
        self.assertEqual(shown, ["README.md", "calculator.py", "tests/__init__.py", "tests/test_calculator.py"])
        app.selectbox(key="file_choice").set_value("calculator.py").run()
        self.assertTrue(any("def calculate(" in block.value for block in app.code))
        self.assertEqual(len(list((Path(self.temp_dir.name) / "runs").glob("*/workspace/calculator.py"))), 1)

    def test_rejecting_the_plan_generates_nothing(self):
        app = self.submit("A command-line calculator for basic arithmetic")
        app.button(key="reject_plan").click().run()

        self.assertFalse(app.exception)
        self.assertIn("no project files were generated", app.info[-1].value)
        self.assertEqual(list((Path(self.temp_dir.name) / "runs").glob("*/workspace")), [])

    def test_unsupported_requirement_cannot_be_approved(self):
        app = self.submit("Build a multiplayer chess game with a leaderboard")

        self.assertFalse(app.exception)
        self.assertIn("No supported project type", " ".join(w.value for w in app.warning))
        self.assertTrue(app.button(key="approve_plan").disabled)

    def test_empty_requirement_shows_error_and_saves_nothing(self):
        app = self.submit("   ")

        self.assertFalse(app.exception)
        self.assertIn("empty", app.error[0].value)
        self.assertEqual(len(app.metric), 0)
        self.assertEqual(history.load_history(self.history_file), [])


if __name__ == "__main__":
    unittest.main()
