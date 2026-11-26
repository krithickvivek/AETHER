"""Shared set-up for the API tests (tests/test_api_*.py).

FAIL FAST, NEVER SKIP: the API's packages are required to run the test suite. If they are missing, importing this
module raises ImportError, so every API test module is reported as an error and the run cannot end in a plain "OK".
Install them with:  python -m pip install -r requirements-api.txt

Every test uses temporary folders for history and runs, a blocked network, and builds its runs with the real AETHER
core (start_project / decide_plan), so no real user data is read or written.
"""

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import fastapi  # noqa: F401
    import httpx  # noqa: F401
    import uvicorn  # noqa: F401
    from fastapi.testclient import TestClient
except ImportError as error:  # deliberately NOT a skip
    raise ImportError("The API tests need the packages in requirements-api.txt "
                      "(run: python -m pip install -r requirements-api.txt). "
                      f"Original error: {error}") from error

from aether.context import SOURCE_ROOT, RunStore
from aether_api.app import create_app
from aether_api.security import SecuritySettings
from aether_ui import history
from aether_ui.workflow import decide_plan, start_project

HOST = "localhost:8765"
BASE_URL = f"http://{HOST}"
API = "/api/v1"
CALCULATOR = "A command-line calculator for basic arithmetic"
TODO = "Build a to-do website where users can log in and save their tasks"
TODO_WITH_API = "A to-do website with login and a public API"   # the to-do template fits, "API" is not supported
AMBIGUOUS = "A to-do list for library staff"                      # names two project types
CALCULATOR_FILES = ["README.md", "calculator.py", "tests/__init__.py", "tests/test_calculator.py"]


def no_network(*args, **kwargs):
    raise OSError("Network access is blocked in tests")


def snapshot_tree(root: Path) -> dict:
    """Every file and folder under `root` with its size and modification time, to prove nothing changed."""
    found = {}
    if not root.exists():
        return found
    for folder, folders, names in os.walk(root):
        for name in folders + names:
            path = Path(folder) / name
            stat = path.stat()
            found[path.relative_to(root).as_posix()] = (path.is_dir(), stat.st_size, stat.st_mtime_ns)
    return found


class ApiTestCase(unittest.TestCase):
    settings = SecuritySettings()

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base = Path(self.temp_dir.name)
        self.runs = self.base / "runs"
        self.history_file = self.base / "history.json"
        patch_env = mock.patch.dict(os.environ, {"AETHER_HISTORY_FILE": str(self.history_file),
                                                 "AETHER_RUNS_DIR": str(self.runs)})
        patch_env.start()
        self.addCleanup(patch_env.stop)
        os.environ.pop("AETHER_PLANNER", None)
        patch_net = mock.patch("urllib.request.urlopen", side_effect=no_network)
        patch_net.start()
        self.addCleanup(patch_net.stop)
        self.app = create_app(self.settings)
        self.client = TestClient(self.app, base_url=BASE_URL)

    # ------------------------------------------------------------------ building runs with the real core

    def make_run(self, requirement: str = CALCULATOR, *, save_history: bool = True, planner=None, approve: bool = False,
                 override: str = ""):
        """Plan a run (stopping at Checkpoint A), optionally save it to history, optionally approve it."""
        result, context = start_project(requirement, planner=planner, store=RunStore())
        entry = history.save_run(result) if save_history and result.ok else None
        if approve:
            decide_plan(context, approved=True, store=RunStore(), override=override)
        return context, entry

    def reject(self, context):
        decide_plan(context, approved=False, store=RunStore(), note="Rejected in a test")

    def run_dir(self, run_id: str) -> Path:
        return self.runs / run_id

    def write_context(self, run_id: str, **changes):
        """Edit a saved run's context.json the way a damaged or newer file would look (test data only)."""
        path = self.run_dir(run_id) / "context.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(changes)
        path.write_text(json.dumps(data), encoding="utf-8")

    # ------------------------------------------------------------------ requests and assertions

    def get(self, path: str, **kwargs):
        return self.client.get(API + path, **kwargs)

    def assert_error(self, response, status: int, code: str):
        self.assertEqual(response.status_code, status, response.text)
        body = response.json()
        self.assertEqual(set(body), {"error"}, body)
        self.assertEqual(set(body["error"]), {"code", "message", "details"}, body)
        self.assertEqual(body["error"]["code"], code, body)
        self.assertTrue(body["error"]["message"])
        self.assert_no_private_text(response)
        return body["error"]

    def assert_no_private_text(self, response):
        """No absolute path (the temporary folders, the home folder, AETHER's folder) or traceback in a response."""
        text = response.text
        for secret in (self.base, Path.home(), SOURCE_ROOT):
            for variant in {str(secret), str(secret).replace("\\", "/"), json.dumps(str(secret))[1:-1]}:
                self.assertNotIn(variant.lower(), text.lower(), f"{variant!r} leaked into the response")
        self.assertNotIn("Traceback", text)
        self.assertIsNone(re.search(r'[A-Za-z]:\\\\', text), "a Windows drive path leaked into the response")
