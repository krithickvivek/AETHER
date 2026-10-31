"""Tests for the web interface's workflow tracing (no Streamlit needed)."""

import unittest

from aether.models import Plan, PlanStep, TaskBrief
from aether.planner import Planner
from aether_ui.workflow import (
    DONE, FAILED, SKIPPED, result_from_dict, result_to_dict, run_workflow, step_origin,
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


if __name__ == "__main__":
    unittest.main()
