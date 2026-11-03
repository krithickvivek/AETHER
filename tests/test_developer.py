"""Tests for the Developer Agent, Checkpoint A, and the Manager -> Planner -> Developer handoff.

Files are generated into temporary folders and checked on disk. Generated code is never run.
"""

import hashlib
import tempfile
import unittest
from pathlib import Path

from aether.agents import PlannerAgent
from aether.context import (
    APPROVED, AWAITING_APPROVAL, CANCELLED, COMPLETED, DONE, FAILED, SKIPPED, SOURCE_ROOT, WAITING, RunStore,
)
from aether.developer import DeveloperAgent, preview
from aether.manager import ManagerAgent
from aether.planner import RuleBasedPlanner
from aether.workspace import Workspace

REQUIREMENTS = {
    "calculator": "A command-line calculator for basic arithmetic",
    "todo_web": "Build a to-do website where users can log in and save their tasks",
    "library_api": "Create a REST API for a library that stores books and members in a database",
}


class DeveloperTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RunStore(Path(self.temp.name))
        self.manager = ManagerAgent(RuleBasedPlanner(), log=lambda message: None)

    def agents(self):
        return [PlannerAgent(RuleBasedPlanner()), DeveloperAgent(self.store)]

    def start(self, requirement):
        return self.manager.run_pipeline(requirement, self.agents(), store=self.store)

    def approve_and_build(self, context):
        self.manager.approve(context, "A", approved=True, store=self.store)
        return self.manager.resume(context, self.agents(), store=self.store)


class CheckpointTests(DeveloperTestCase):
    def test_run_pauses_for_plan_approval_before_any_file_is_written(self):
        context = self.start(REQUIREMENTS["todo_web"])

        self.assertEqual(context.status, AWAITING_APPROVAL)
        self.assertIsNotNone(context.plan)
        self.assertEqual(context.files, [])
        self.assertFalse(self.store.workspace_dir(context.run_id).exists())
        waiting = context.events[-1]
        self.assertEqual((waiting.agent, waiting.status), ("Manager", WAITING))
        self.assertIn("Checkpoint A", waiting.action)
        self.assertIn("To-do web app: 13 files planned", waiting.detail)
        self.assertEqual(self.store.load(context.run_id).status, AWAITING_APPROVAL)

    def test_approval_generates_files_and_completes(self):
        context = self.approve_and_build(self.start(REQUIREMENTS["todo_web"]))

        self.assertEqual(context.status, COMPLETED)
        self.assertEqual(context.approvals[0].decision, APPROVED)
        self.assertEqual([(e.agent, e.status) for e in context.events],
                         [("Manager", DONE), ("Planner", DONE), ("Manager", WAITING), ("User", DONE),
                          ("Developer", DONE)])
        self.assertEqual(context.project_type, "todo_web")
        self.assertEqual(self.store.load(context.run_id), context)

    def test_rejection_cancels_without_generating_files(self):
        context = self.start(REQUIREMENTS["calculator"])
        self.manager.approve(context, "A", approved=False, note="Wrong scope", store=self.store)
        context = self.manager.resume(context, self.agents(), store=self.store)

        self.assertEqual(context.status, CANCELLED)
        self.assertEqual(context.files, [])
        self.assertEqual(context.events[-1].agent, "Developer")
        self.assertEqual(context.events[-1].status, SKIPPED)
        self.assertIn("Wrong scope", context.events[-2].detail)
        self.assertFalse(self.store.workspace_dir(context.run_id).exists())

    def test_cannot_approve_a_run_that_is_not_waiting(self):
        context = self.approve_and_build(self.start(REQUIREMENTS["calculator"]))
        with self.assertRaises(ValueError):
            self.manager.approve(context, "A", approved=True)
        with self.assertRaises(ValueError):
            self.manager.resume(context, self.agents())

    def test_planner_is_not_run_again_on_resume(self):
        context = self.start(REQUIREMENTS["calculator"])
        plan = context.plan
        context = self.approve_and_build(context)
        self.assertIs(context.plan, plan)
        self.assertEqual(sum(1 for e in context.events if e.agent == "Planner"), 1)


class GeneratedFilesTests(DeveloperTestCase):
    def test_all_three_templates_are_written_with_checksums_and_coverage(self):
        for key, requirement in REQUIREMENTS.items():
            with self.subTest(template=key):
                context = self.approve_and_build(self.start(requirement))
                workspace = Workspace(self.store.workspace_dir(context.run_id))

                self.assertEqual(context.status, COMPLETED)
                self.assertEqual(context.project_type, key)
                self.assertEqual(sorted(r.path for r in context.files), workspace.list_files())
                for record in context.files:
                    data = workspace.read_text(record.path).encode("utf-8")
                    self.assertEqual(record.sha256, hashlib.sha256(data).hexdigest())
                    self.assertEqual(record.size, len(data))
                    self.assertEqual(record.author, "Developer")
                covered = set().union(*(set(r.covers) for r in context.files))
                self.assertEqual(covered, set(range(1, len(context.plan.steps) + 1)))
                self.assertIn(requirement, workspace.read_text("README.md"))

    def test_files_are_written_outside_aether_source(self):
        context = self.approve_and_build(self.start(REQUIREMENTS["library_api"]))
        folder = self.store.workspace_dir(context.run_id).resolve()
        self.assertFalse(folder.is_relative_to(SOURCE_ROOT))
        self.assertTrue((folder / "library_api" / "api.py").is_file())

    def test_run_store_refuses_a_folder_inside_aether(self):
        with self.assertRaisesRegex(ValueError, "inside AETHER"):
            RunStore(SOURCE_ROOT / "generated")

    def test_unsupported_requirement_fails_clearly_and_writes_nothing(self):
        context = self.approve_and_build(self.start("Build a multiplayer chess game with a leaderboard"))

        self.assertEqual(context.status, FAILED)
        developer = context.events[-1]
        self.assertEqual((developer.agent, developer.status), ("Developer", FAILED))
        self.assertIn("Unsupported requirement", developer.detail)
        self.assertIn("No supported project type", context.notes["developer_unsupported"])
        self.assertEqual(context.files, [])

    def test_unsupported_features_and_uncovered_steps_are_reported(self):
        context = self.approve_and_build(self.start("A to-do website with login and a public API"))

        self.assertEqual(context.status, COMPLETED)
        self.assertEqual(context.notes["developer_unsupported"], "api")
        self.assertIn("developer_uncovered_steps", context.notes)
        self.assertIn("not covered: api", context.events[-1].detail)

    def test_preview_lists_planned_files_before_generation(self):
        context = self.start(REQUIREMENTS["calculator"])
        plan_preview = preview(context)
        self.assertEqual(plan_preview.template.key, "calculator")
        self.assertEqual(plan_preview.files, ["calculator.py", "tests/__init__.py", "tests/test_calculator.py",
                                              "README.md"])
        self.assertEqual(plan_preview.uncovered_steps, [])


if __name__ == "__main__":
    unittest.main()
