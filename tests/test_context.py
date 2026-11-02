"""Tests for the shared ProjectContext, its activity events, and saving runs to disk."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aether.context import DONE, FAILED, Approval, FileRecord, ProjectContext, RunStore
from aether.models import Plan, PlanStep, TaskBrief


def sample_context() -> ProjectContext:
    context = ProjectContext.new("  Build a  to-do website  ")
    context.brief = TaskBrief("Build a to-do website", ["web_ui"])
    context.plan = Plan("Build a to-do website", "RuleBasedPlanner", [PlanStep("Design", "Sketch the pages")])
    context.files.append(FileRecord("app.py", "Developer", "abc123", 42))
    context.approvals.append(Approval("plan", "approved", "2026-11-01T10:00:00"))
    context.notes["planner_fallback"] = "timed out"
    context.record("Manager", "Understand requirement", DONE, "1 feature detected")
    context.record("Planner", "Build development plan", FAILED, "model unavailable", duration_ms=12.5)
    return context


class ProjectContextTests(unittest.TestCase):
    def test_new_context_cleans_requirement_and_gets_an_id(self):
        context = ProjectContext.new("  Build a  to-do website  ")
        self.assertEqual(context.requirement, "Build a to-do website")
        self.assertTrue(context.run_id.isalnum())
        self.assertNotEqual(context.run_id, ProjectContext.new("x").run_id)

    def test_record_adds_timestamped_events_in_order(self):
        context = sample_context()
        self.assertEqual([e.agent for e in context.events], ["Manager", "Planner"])
        self.assertEqual(context.events[1].status, FAILED)
        self.assertEqual(context.events[1].duration_ms, 12.5)
        self.assertTrue(context.events[0].time)

    def test_round_trip_through_dict_keeps_everything(self):
        context = sample_context()
        restored = ProjectContext.from_dict(json.loads(json.dumps(context.to_dict())))
        self.assertEqual(restored, context)

    def test_minimal_saved_data_still_loads(self):
        restored = ProjectContext.from_dict({"run_id": "abc", "requirement": "x", "created_at": "t"})
        self.assertIsNone(restored.plan)
        self.assertEqual(restored.events, [])


class RunStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RunStore(Path(self.temp.name))

    def test_save_and_load_run(self):
        context = sample_context()
        folder = self.store.save(context)
        self.assertEqual(self.store.load(context.run_id), context)
        events = (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line)["agent"] for line in events], ["Manager", "Planner"])
        self.assertFalse(list(folder.glob("*.tmp")))

    def test_list_runs_and_workspace_folder(self):
        context = sample_context()
        self.store.save(context)
        self.assertEqual(self.store.list_runs(), [context.run_id])
        self.assertEqual(self.store.workspace_dir(context.run_id).parent, self.store.run_dir(context.run_id))

    def test_no_runs_folder_means_no_runs(self):
        self.assertEqual(RunStore(Path(self.temp.name) / "missing").list_runs(), [])

    def test_run_id_cannot_escape_the_runs_folder(self):
        with self.assertRaises(ValueError):
            self.store.run_dir("../outside")

    def test_environment_variable_sets_the_folder(self):
        with mock.patch.dict(os.environ, {"AETHER_RUNS_DIR": self.temp.name}):
            self.assertEqual(RunStore().root, Path(self.temp.name))


if __name__ == "__main__":
    unittest.main()
