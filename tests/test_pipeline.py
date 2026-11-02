"""Tests for the agent interface and the Manager's multi-agent pipeline.

Only the Planner exists as an agent so far; small fake agents stand in for later ones.
No network calls are made.
"""

import tempfile
import unittest
import urllib.error
from pathlib import Path

from aether.agents import Agent, AgentResult, PlannerAgent
from aether.context import COMPLETED, DONE, FAILED, SKIPPED, RunStore
from aether.fallback import FallbackPlanner
from aether.manager import ManagerAgent
from aether.ollama_planner import OllamaPlanner
from aether.planner import RuleBasedPlanner

REQUIREMENT = "Build a to-do website where users can log in and save tasks"


def quiet_manager() -> ManagerAgent:
    return ManagerAgent(planner=RuleBasedPlanner(), log=lambda message: None)


class RecordingAgent(Agent):
    """A stand-in for a future agent. Remembers what it saw and returns a fixed result."""

    def __init__(self, name, result=None, error=None):
        self.name, self.action = name, f"{name} work"
        self.result = result or AgentResult(DONE, "ok")
        self.error = error
        self.saw_plan = None

    def run(self, context):
        self.saw_plan = context.plan
        if self.error:
            raise self.error
        return self.result


class PipelineTests(unittest.TestCase):
    def test_planner_only_pipeline_matches_the_existing_run(self):
        context = quiet_manager().run_pipeline(REQUIREMENT, [PlannerAgent(RuleBasedPlanner())])
        expected = quiet_manager().run(REQUIREMENT)

        self.assertEqual(context.status, COMPLETED)
        self.assertEqual(context.plan, expected)
        self.assertEqual(context.brief.features, ["authentication", "database", "web_ui"])
        self.assertEqual([(e.agent, e.status) for e in context.events], [("Manager", DONE), ("Planner", DONE)])
        self.assertIn("18 steps from RuleBasedPlanner", context.events[-1].detail)
        self.assertIsNotNone(context.events[-1].duration_ms)

    def test_later_agents_receive_the_shared_context(self):
        later = RecordingAgent("Developer")
        context = quiet_manager().run_pipeline(REQUIREMENT, [PlannerAgent(RuleBasedPlanner()), later])
        self.assertIs(later.saw_plan, context.plan)
        self.assertEqual(context.events[-1].agent, "Developer")

    def test_ai_planner_fallback_is_logged_and_noted(self):
        failing = OllamaPlanner(send=lambda *args: (_ for _ in ()).throw(urllib.error.URLError("refused")))
        planner = FallbackPlanner(failing, RuleBasedPlanner())
        context = quiet_manager().run_pipeline(REQUIREMENT, [PlannerAgent(planner)])

        self.assertEqual(context.status, COMPLETED)
        self.assertEqual(context.plan.planner_name, "RuleBasedPlanner")
        self.assertEqual([(e.action, e.status) for e in context.events[1:]],
                         [("Build plan with local AI model", FAILED), ("Build development plan", DONE)])
        self.assertIn("Could not reach Ollama", context.notes["planner_fallback"])

    def test_crashing_agent_fails_the_run_and_skips_the_rest(self):
        agents = [PlannerAgent(RuleBasedPlanner()), RecordingAgent("Developer", error=RuntimeError("disk full")),
                  RecordingAgent("Reviewer")]
        context = quiet_manager().run_pipeline(REQUIREMENT, agents)

        self.assertEqual(context.status, FAILED)
        self.assertEqual([(e.agent, e.status) for e in context.events],
                         [("Manager", DONE), ("Planner", DONE), ("Developer", FAILED), ("Reviewer", SKIPPED)])
        self.assertIn("RuntimeError: disk full", context.events[2].detail)
        self.assertIsNotNone(context.plan)  # work finished before the failure is kept

    def test_agent_reporting_failure_stops_the_run(self):
        agents = [RecordingAgent("Developer", AgentResult(FAILED, "template not found")), RecordingAgent("Tester")]
        context = quiet_manager().run_pipeline(REQUIREMENT, agents)
        self.assertEqual(context.status, FAILED)
        self.assertEqual([e.status for e in context.events], [DONE, FAILED, SKIPPED])

    def test_agent_cannot_change_protected_fields(self):
        sneaky = RecordingAgent("Developer", AgentResult(DONE, "ok", updates={"status": "completed"}))
        context = quiet_manager().run_pipeline(REQUIREMENT, [sneaky])
        self.assertEqual(context.status, FAILED)
        self.assertIn("may not set", context.events[-1].detail)

    def test_empty_requirement_fails_before_any_agent_runs(self):
        planner_agent = RecordingAgent("Planner")
        context = quiet_manager().run_pipeline("   ", [planner_agent])
        self.assertEqual(context.status, FAILED)
        self.assertIsNone(planner_agent.saw_plan)
        self.assertEqual([(e.agent, e.status) for e in context.events], [("Manager", FAILED), ("Planner", SKIPPED)])
        self.assertIn("empty", context.events[0].detail)

    def test_run_is_saved_after_every_step(self):
        with tempfile.TemporaryDirectory() as folder:
            store = RunStore(Path(folder))
            context = quiet_manager().run_pipeline(REQUIREMENT, [PlannerAgent(RuleBasedPlanner())], store=store)
            self.assertEqual(store.load(context.run_id), context)

    def test_existing_run_method_is_unchanged(self):
        messages = []
        plan = ManagerAgent(planner=RuleBasedPlanner(), log=messages.append).run(REQUIREMENT)
        self.assertEqual(len(plan.steps), 18)
        self.assertIn("[Manager] Handing off to RuleBasedPlanner...", messages)


if __name__ == "__main__":
    unittest.main()
