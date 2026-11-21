"""Tests for what the Developer Agent does when it cannot write the project files (Task 009, group A).

Failures are produced with temporary folders only, using the Workspace's real limits (MAX_FILE_BYTES,
MAX_FILES) wherever possible, and one injected OSError for the disk-error branch. No real user data, no
network, and generated code is never run.

Pinned current behaviour (not changed here): when a write fails part-way, the files already written stay on
disk and in the run's record. See the Workspace page tests for how the web app shows that (known issue K38).
"""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aether import workspace
from aether.agents import PlannerAgent
from aether.context import FAILED, ProjectContext, RunStore
from aether.developer import DeveloperAgent
from aether.manager import ManagerAgent
from aether.planner import RuleBasedPlanner
from aether.workspace import Workspace

CALCULATOR = "A command-line calculator for basic arithmetic"
CALCULATOR_FILES = ["calculator.py", "tests/__init__.py", "tests/test_calculator.py", "README.md"]


class DeveloperWriteFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RunStore(Path(self.temp.name))
        self.manager = ManagerAgent(RuleBasedPlanner(), log=lambda message: None)

    # ------------------------------------------------------------------ helpers

    def agents(self):
        return [PlannerAgent(RuleBasedPlanner()), DeveloperAgent(self.store)]

    def build(self, requirement: str = CALCULATOR) -> ProjectContext:
        """Run to Checkpoint A, approve, and let the Developer try to write the files."""
        context = self.manager.run_pipeline(requirement, self.agents(), store=self.store)
        self.manager.approve(context, "A", approved=True, store=self.store)
        return self.manager.resume(context, self.agents(), store=self.store)

    def files_on_disk(self, context: ProjectContext) -> list[str]:
        return Workspace(self.store.workspace_dir(context.run_id)).list_files()

    def assert_developer_failed(self, context: ProjectContext) -> str:
        """The run failed at the Developer and nothing came after it. Returns the failure detail."""
        self.assertEqual(context.status, FAILED)
        event = context.events[-1]
        self.assertEqual((event.agent, event.status), ("Developer", FAILED))
        return event.detail

    # ------------------------------------------------------------------ the write-failure path

    def test_first_file_refused_fails_the_run_and_writes_nothing(self):
        with mock.patch.object(workspace, "MAX_FILE_BYTES", 10):  # every template file is larger than this
            context = self.build()

        detail = self.assert_developer_failed(context)
        self.assertTrue(detail.startswith("Could not write project files:"), detail)
        self.assertIn("(limit 10)", detail)
        self.assertEqual(context.files, [])
        self.assertEqual(context.project_type, "calculator")  # still recorded for the failed attempt
        self.assertEqual(self.files_on_disk(context), [])

    def test_a_later_file_refused_keeps_the_files_already_written(self):
        with mock.patch.object(workspace, "MAX_FILES", 1):  # the second new file is refused
            context = self.build()

        detail = self.assert_developer_failed(context)
        self.assertIn("Could not write project files:", detail)
        self.assertIn("maximum of 1 files", detail)
        self.assertEqual([f.path for f in context.files], ["calculator.py"])
        self.assertEqual(context.project_type, "calculator")
        self.assertEqual(self.files_on_disk(context), ["calculator.py"])  # pinned: the partial project stays
        record, written = context.files[0], (self.store.workspace_dir(context.run_id) / "calculator.py").read_bytes()
        self.assertEqual(record.sha256, hashlib.sha256(written).hexdigest())
        self.assertEqual(record.size, len(written))

    def test_a_disk_error_part_way_is_reported_the_same_way(self):
        real_write = Workspace.write_text
        attempted = []

        def fail_on_the_second_file(self_, relative, content, author):
            attempted.append(relative)
            if len(attempted) == 2:
                raise OSError("disk full")
            return real_write(self_, relative, content, author)

        with mock.patch.object(Workspace, "write_text", fail_on_the_second_file):
            context = self.build()

        self.assertEqual(self.assert_developer_failed(context), "Could not write project files: disk full")
        self.assertEqual(attempted, CALCULATOR_FILES[:2])  # it stopped at the failure
        self.assertEqual([f.path for f in context.files], ["calculator.py"])
        self.assertEqual(self.files_on_disk(context), ["calculator.py"])

    def test_a_failed_run_is_saved_and_reloaded_with_its_partial_files(self):
        with mock.patch.object(workspace, "MAX_FILES", 1):
            context = self.build()

        saved = self.store.load(context.run_id)
        self.assertEqual(saved.status, FAILED)
        self.assertEqual([f.path for f in saved.files], ["calculator.py"])
        self.assertEqual(saved.project_type, "calculator")
        self.assertEqual((saved.events[-1].agent, saved.events[-1].status, saved.events[-1].detail),
                         ("Developer", FAILED, context.events[-1].detail))
        self.assertEqual(saved.load_warnings, [])

    def test_the_developer_does_not_swallow_unexpected_errors_the_manager_records_them(self):
        with mock.patch.object(Workspace, "write_text", side_effect=RuntimeError("boom")):
            context = self.build()

        detail = self.assert_developer_failed(context)
        self.assertEqual(detail, "RuntimeError: boom")  # recorded by the Manager, not the write-failure message
        self.assertEqual(context.files, [])

        planned = self.manager.run_pipeline(CALCULATOR, self.agents(), store=self.store)
        with mock.patch.object(Workspace, "write_text", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                DeveloperAgent(self.store).run(planned)  # called directly, the error propagates

    # ------------------------------------------------------------------ no approved plan

    def test_no_brief_or_plan_fails_without_writing_anything(self):
        context = ProjectContext.new("A command-line calculator")  # no brief and no plan yet
        result = DeveloperAgent(self.store).run(context)

        self.assertEqual(result.status, FAILED)
        self.assertEqual(result.detail, "There is no approved plan to build from.")
        self.assertEqual(result.updates, {})
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])  # not even a run folder was created


if __name__ == "__main__":
    unittest.main()
