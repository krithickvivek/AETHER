"""Tests for the Manager -> Planner workflow.

Run from the project folder with:
    python -m unittest discover -s tests -v
"""

import unittest

from aether.manager import ManagerAgent, detect_features
from aether.models import Plan, PlanStep, TaskBrief
from aether.planner import BASE_STEPS, PHASES, Planner, RuleBasedPlanner


def quiet_manager(planner: Planner) -> ManagerAgent:
    """A Manager that does not print log messages during tests."""
    return ManagerAgent(planner=planner, log=lambda message: None)


class FakePlanner(Planner):
    """Records what it receives, so we can prove the Manager hands work off."""

    name = "FakePlanner"

    def __init__(self):
        self.received = None

    def create_plan(self, brief: TaskBrief) -> Plan:
        self.received = brief
        return Plan(
            requirement=brief.requirement,
            planner_name=self.name,
            steps=[PlanStep("Requirements", "fake step")],
        )


class FeatureDetectionTests(unittest.TestCase):
    def test_detects_all_features(self):
        text = "A website where users log in, save notes, and call a REST API"
        self.assertEqual(detect_features(text), ["authentication", "database", "web_ui", "api"])

    def test_plain_requirement_has_no_features(self):
        self.assertEqual(detect_features("A command-line calculator"), [])

    def test_matches_whole_words_only(self):
        # "capital" contains "api" but is not about APIs.
        self.assertEqual(detect_features("Show the capital of each country"), [])


class ManagerTests(unittest.TestCase):
    def test_manager_hands_off_to_planner(self):
        planner = FakePlanner()
        plan = quiet_manager(planner).run("Build a website   with login")

        self.assertIsNotNone(planner.received, "Manager never called the planner")
        self.assertEqual(planner.received.requirement, "Build a website with login")
        self.assertEqual(planner.received.features, ["authentication", "web_ui"])
        self.assertEqual(plan.planner_name, "FakePlanner")

    def test_empty_requirement_is_rejected(self):
        with self.assertRaises(ValueError):
            quiet_manager(FakePlanner()).run("   ")


class RuleBasedPlannerTests(unittest.TestCase):
    def test_plain_requirement_gets_base_steps_only(self):
        plan = RuleBasedPlanner().create_plan(TaskBrief("A command-line calculator"))
        self.assertEqual(len(plan.steps), len(BASE_STEPS))

    def test_authentication_adds_login_steps(self):
        plan = RuleBasedPlanner().create_plan(TaskBrief("x", features=["authentication"]))
        descriptions = [step.description for step in plan.steps]
        self.assertTrue(any("login" in text for text in descriptions))

    def test_steps_follow_phase_order(self):
        brief = TaskBrief("x", features=["authentication", "database", "web_ui", "api"])
        plan = RuleBasedPlanner().create_plan(brief)
        phase_positions = [PHASES.index(step.phase) for step in plan.steps]
        self.assertEqual(phase_positions, sorted(phase_positions))


if __name__ == "__main__":
    unittest.main()
