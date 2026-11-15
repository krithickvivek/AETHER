"""Tests for the web interface's workflow tracing (no Streamlit needed)."""

import tempfile
import unittest
from pathlib import Path

from aether.context import RunStore
from aether.models import Plan, PlanStep, TaskBrief
from aether.planner import Planner
from aether_ui.workflow import (
    CURRENT, DONE, FAILED, PENDING, SKIPPED, decide_plan, progress_steps, result_from_dict, result_to_dict,
    run_workflow, start_project, step_origin,
)


class BrokenPlanner(Planner):
    name = "BrokenPlanner"

    def create_plan(self, brief: TaskBrief) -> Plan:
        raise RuntimeError("planner crashed")


class RunWorkflowTests(unittest.TestCase):
    def test_successful_run_records_real_handoff(self):
        result = run_workflow("Build a to-do website where users can log in and save tasks")

        self.assertTrue(result.ok)
        self.assertEqual([s.status for s in result.stages], [DONE, DONE, DONE])
        self.assertEqual(result.brief.features, ["authentication", "database", "web_ui"])
        self.assertEqual(len(result.plan.steps), 18)
        self.assertTrue(any("Handing off to RuleBasedPlanner" in line for line in result.log))

    def test_empty_requirement_stops_before_handoff(self):
        result = run_workflow("   ")

        self.assertFalse(result.ok)
        self.assertIn("empty", result.error)
        self.assertIsNone(result.brief)
        self.assertEqual([s.status for s in result.stages], [FAILED, SKIPPED, SKIPPED])

    def test_planner_crash_is_reported_not_raised(self):
        result = run_workflow("A website", planner=BrokenPlanner())

        self.assertFalse(result.ok)
        self.assertIn("RuntimeError: planner crashed", result.error_details)
        self.assertEqual([s.status for s in result.stages], [DONE, DONE, FAILED])

    def test_result_survives_round_trip(self):
        result = run_workflow("A REST API with a database")
        restored = result_from_dict(result_to_dict(result))

        self.assertEqual(restored.plan, result.plan)
        self.assertEqual(restored.brief, result.brief)
        self.assertEqual(restored.stages, result.stages)


class StepOriginTests(unittest.TestCase):
    def test_core_and_feature_steps(self):
        core = PlanStep("Design", "Choose the technology stack")
        login = PlanStep("Testing", "Test login with valid and invalid credentials")
        self.assertEqual(step_origin(core, "RuleBasedPlanner"), "core")
        self.assertEqual(step_origin(login, "RuleBasedPlanner"), "authentication")

    def test_unknown_planner_has_no_origin(self):
        step = PlanStep("Design", "Choose the technology stack")
        self.assertIsNone(step_origin(step, "SomeOtherPlanner"))


class ProgressStepsTests(unittest.TestCase):
    """The workflow indicator reflects the real run state. Files go to a temporary folder only."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RunStore(Path(self.temp.name))

    def statuses(self, result, context):
        steps = progress_steps(result, context)
        self.assertEqual([s.title for s in steps], ["Requirement", "Plan", "Approval", "Generated files"])
        return [s.status for s in steps]

    def test_run_waiting_at_checkpoint_highlights_approval(self):
        result, context = start_project("A command-line calculator for basic arithmetic", store=self.store)
        self.assertEqual(self.statuses(result, context), [DONE, DONE, CURRENT, PENDING])
        self.assertEqual(progress_steps(result, context)[2].detail, "Waiting for you")

    def test_approved_run_completes_every_stage(self):
        result, context = start_project("A command-line calculator for basic arithmetic", store=self.store)
        context = decide_plan(context, approved=True, store=self.store)
        self.assertEqual(self.statuses(result, context), [DONE, DONE, DONE, DONE])
        self.assertEqual(progress_steps(result, context)[3].detail, "4 files")

    def test_rejected_run_marks_approval_failed_and_generates_nothing(self):
        result, context = start_project("A command-line calculator for basic arithmetic", store=self.store)
        context = decide_plan(context, approved=False, store=self.store)
        self.assertEqual(self.statuses(result, context), [DONE, DONE, FAILED, PENDING])

    def test_developer_failure_marks_generated_files_failed(self):
        result, context = start_project("Build a multiplayer chess game with a leaderboard", store=self.store)
        context = decide_plan(context, approved=True, store=self.store)  # no template fits
        self.assertEqual(self.statuses(result, context), [DONE, DONE, DONE, FAILED])

    def test_rejected_requirement_and_planner_crash(self):
        empty, context = start_project("   ", store=self.store)
        self.assertEqual(self.statuses(empty, context), [FAILED, PENDING, PENDING, PENDING])
        crashed, context = start_project("A website", planner=BrokenPlanner(), store=self.store)
        self.assertEqual(self.statuses(crashed, context), [DONE, FAILED, PENDING, PENDING])

    def test_run_without_saved_context_leaves_later_stages_pending(self):
        result = run_workflow("A REST API with a database")
        self.assertEqual(self.statuses(result, None), [DONE, DONE, PENDING, PENDING])


if __name__ == "__main__":
    unittest.main()
