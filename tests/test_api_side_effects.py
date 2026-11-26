"""Read endpoints must have no side effects (C1): no folder is created, nothing is written, nothing is executed.

Workspace.__init__ creates its folder, so the API loads and validates the run first and only builds a Workspace
for a run that has recorded files AND an existing workspace folder. These tests prove it, request by request."""

import os
import shutil
import subprocess
import unittest
from unittest import mock

try:
    from api_support import CALCULATOR, ApiTestCase, snapshot_tree
except ImportError as error:
    if "requirements-api.txt" in str(error):
        raise
    from tests.api_support import CALCULATOR, ApiTestCase, snapshot_tree

from aether.context import RunStore
from aether.workspace import Workspace
from aether_ui import history

UNKNOWN_RUN = "abcdef123456"


def run_endpoints(run_id: str) -> list[str]:
    return [f"/runs/{run_id}", f"/runs/{run_id}/checkpoint", f"/runs/{run_id}/files",
            f"/runs/{run_id}/files/content?path=calculator.py", f"/runs/{run_id}/files/content?path=../x.py",
            f"/runs/{run_id}/plan.md"]


class WorkspaceIsNeverCreatedByReadingTests(ApiTestCase):
    def sweep(self, run_id: str) -> dict:
        return {path: self.get(path).status_code for path in run_endpoints(run_id)}

    def test_a_missing_run_creates_no_folder_at_all(self):
        with mock.patch("aether_api.repository.Workspace", wraps=Workspace) as constructed:
            statuses = self.sweep(UNKNOWN_RUN)
        self.assertEqual(set(statuses.values()), {404}, statuses)
        self.assertFalse(self.runs.exists(), "not even the runs folder may be created")
        self.assertFalse((self.runs / UNKNOWN_RUN).exists())
        constructed.assert_not_called()

    def test_many_unknown_and_odd_ids_create_nothing(self):
        for run_id in ("000000000000", "ffffffffffff", "x", "bad.id", "a" * 64, "UPPER123", "caf%C3%A9"):
            for path in run_endpoints(run_id):
                self.get(path)
        self.assertFalse(self.runs.exists())

    def test_a_run_with_no_files_gets_no_workspace_folder(self):
        context, _ = self.make_run(CALCULATOR)  # waiting at Checkpoint A: the Developer has not written anything
        before = snapshot_tree(self.base)
        self.assertFalse((self.run_dir(context.run_id) / "workspace").exists())
        with mock.patch("aether_api.repository.Workspace", wraps=Workspace) as constructed:
            statuses = self.sweep(context.run_id)
        self.assertEqual(statuses[f"/runs/{context.run_id}/files/content?path=calculator.py"], 404)
        self.assertEqual(statuses[f"/runs/{context.run_id}"], 200)
        self.assertFalse((self.run_dir(context.run_id) / "workspace").exists())
        constructed.assert_not_called()
        self.assertEqual(snapshot_tree(self.base), before, "nothing was created, changed or removed")

    def test_a_cancelled_run_gets_no_workspace_folder_either(self):
        context, _ = self.make_run(CALCULATOR)
        self.reject(context)
        before = snapshot_tree(self.base)
        with mock.patch("aether_api.repository.Workspace", wraps=Workspace) as constructed:
            self.sweep(context.run_id)
        constructed.assert_not_called()
        self.assertEqual(snapshot_tree(self.base), before)

    def test_a_failed_planning_run_gets_no_workspace_folder(self):
        context, _ = self.make_run("   ")
        before = snapshot_tree(self.base)
        with mock.patch("aether_api.repository.Workspace", wraps=Workspace) as constructed:
            self.sweep(context.run_id)
        constructed.assert_not_called()
        self.assertEqual(snapshot_tree(self.base), before)

    def test_a_workspace_is_built_only_to_read_a_file_and_only_when_its_folder_exists(self):
        context, _ = self.make_run(CALCULATOR, approve=True)
        with mock.patch("aether_api.repository.Workspace", wraps=Workspace) as constructed:
            self.get(f"/runs/{context.run_id}")
            self.get(f"/runs/{context.run_id}/files")
            self.get(f"/runs/{context.run_id}/plan.md")
            self.get(f"/runs/{context.run_id}/checkpoint")
            constructed.assert_not_called()  # listing and viewing a run never need one
            self.assertEqual(self.get(f"/runs/{context.run_id}/files/content?path=calculator.py").status_code, 200)
            self.assertEqual(constructed.call_count, 1)

    def test_a_workspace_folder_that_was_deleted_is_not_recreated(self):
        context, _ = self.make_run(CALCULATOR, approve=True)
        workspace_dir = self.run_dir(context.run_id) / "workspace"
        shutil.rmtree(workspace_dir)
        before = snapshot_tree(self.base)
        with mock.patch("aether_api.repository.Workspace", wraps=Workspace) as constructed:
            self.sweep(context.run_id)
            self.get(f"/runs/{context.run_id}/files/content?path=README.md")
        constructed.assert_not_called()
        self.assertFalse(workspace_dir.exists())
        self.assertEqual(snapshot_tree(self.base), before)

    def test_the_whole_sweep_of_a_completed_run_changes_nothing_on_disk(self):
        context, entry = self.make_run(CALCULATOR, approve=True)
        before = snapshot_tree(self.base)
        contents = {p: p.read_bytes() for p in self.base.rglob("*") if p.is_file()}
        paths = run_endpoints(context.run_id) + ["/health", "/config", "/history", f"/history/{entry['id']}",
                                                  f"/runs/{context.run_id}/files/content?path=tests/test_calculator.py",
                                                  f"/runs/{context.run_id}/checkpoint?override=todo_web"]
        for path in paths:
            self.get(path)
        self.assertEqual(snapshot_tree(self.base), before, "sizes and modification times are unchanged")
        self.assertEqual({p: p.read_bytes() for p in self.base.rglob("*") if p.is_file()}, contents)


class NothingIsWrittenOrExecutedTests(ApiTestCase):
    def test_no_writer_in_the_core_is_ever_called_by_a_read(self):
        """Every function that can write a run, history or a generated file raises if it is called."""
        context, entry = self.make_run(CALCULATOR, approve=True)
        waiting, _ = self.make_run(CALCULATOR)
        paths = ["/health", "/config", "/history", f"/history/{entry['id']}", "/planners/ollama/status",
                 *run_endpoints(context.run_id), *run_endpoints(waiting.run_id), f"/runs/{waiting.run_id}/checkpoint?override=x"]
        forbidden = ["aether.context.RunStore.save", "aether.context._write_atomic", "aether_ui.history.save_run",
                     "aether_ui.history.clear_history", "aether_ui.history._write", "aether.workspace.Workspace.write_text",
                     "aether.manager.ManagerAgent.approve", "aether.manager.ManagerAgent.resume",
                     "aether.manager.ManagerAgent.run_pipeline", "aether.developer.DeveloperAgent.run",
                     "shutil.copy2", "shutil.rmtree", "os.remove", "os.replace", "os.unlink",
                     "subprocess.Popen", "subprocess.run", "os.system"]
        patchers = [mock.patch(name, side_effect=AssertionError(f"{name} was called by a read endpoint"))
                    for name in forbidden]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        for path in paths:
            with self.subTest(path=path):
                self.assertIn(self.get(path).status_code, (200, 404, 422), path)  # an AssertionError would be a 500

    def test_a_damaged_history_is_never_rewritten_or_copied_aside_by_reading(self):
        self.history_file.write_bytes(b"[{broken")
        with mock.patch("aether_ui.history._write", side_effect=AssertionError("wrote history")), \
                mock.patch("shutil.copy2", side_effect=AssertionError("copied history aside")):
            self.assertEqual(self.get("/history").json()["file_state"], "damaged")
        self.assertEqual(self.history_file.read_bytes(), b"[{broken")
        self.assertEqual(sorted(p.name for p in self.base.iterdir()), ["history.json"])

    def test_importing_the_api_does_not_import_streamlit_or_the_app(self):
        """The API must stay usable without Streamlit and must never import app.py (which starts Streamlit)."""
        code = ("import sys\n"
                "import aether_api.app\n"
                "bad = sorted(m for m in sys.modules if m == 'streamlit' or m == 'app' or m == 'aether_ui.theme')\n"
                "print('LOADED', bad)\n")
        done = subprocess.run([os.sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=120,
                              cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("LOADED []", done.stdout)

    def test_the_default_stores_are_untouched_when_the_environment_is_isolated(self):
        """The tests' own safety net: the isolated stores are the ones in use, never the real ones."""
        self.assertEqual(history.history_path(), self.history_file)
        self.assertEqual(RunStore().root, self.runs)


if __name__ == "__main__":
    unittest.main()
