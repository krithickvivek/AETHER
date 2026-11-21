"""Regression tests for the single execution path (Task 005).

ManagerAgent.run_pipeline() is the only orchestration implementation. ManagerAgent.run() (plan only) and the
command-line app go through it. These tests pin the behaviour that existed before the two paths were unified.
"""

import ast
import contextlib
import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import main
from aether.agents import Agent, AgentResult, PlannerAgent
from aether.context import (
    AWAITING_APPROVAL, CANCELLED, COMPLETED, DONE, FAILED, SKIPPED, WAITING, RunStore,
)
from aether.fallback import FallbackPlanner
from aether.manager import ManagerAgent
from aether.models import Plan, PlanStep, TaskBrief
from aether.ollama_planner import OllamaPlanner
from aether.planner import Planner, RuleBasedPlanner

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENT = "Build a to-do website where users can log in and save tasks"


class CountingPlanner(Planner):
    """A real RuleBasedPlanner that counts how many times it is asked for a plan."""

    name = "CountingPlanner"

    def __init__(self):
        self.calls = 0

    def create_plan(self, brief: TaskBrief) -> Plan:
        self.calls += 1
        return RuleBasedPlanner().create_plan(brief)


class StepAgent(Agent):
    """A stand-in agent that records every time it runs."""

    def __init__(self, name, status=DONE, error=None, checkpoint=None):
        self.name, self.action = name, f"{name} work"
        self.status, self.error = status, error
        self.checkpoint = checkpoint
        self.checkpoint_title = f"Approve before {name}"
        self.runs = 0

    def preview(self, context):
        return f"{self.name} will run"

    def run(self, context):
        self.runs += 1
        if self.error:
            raise self.error
        return AgentResult(self.status, f"{self.name} ran")


def manager(log=None) -> ManagerAgent:
    return ManagerAgent(planner=RuleBasedPlanner(), log=log or (lambda message: None))


def run_cli(args, planner=None, typed=None):
    """Run main.main() and return (exit code, printed text)."""
    out = io.StringIO()
    patches = [mock.patch.object(main, "build_planner", return_value=planner)] if planner else []
    if typed is not None:
        patches.append(mock.patch("builtins.input", return_value=typed))
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        stack.enter_context(contextlib.redirect_stdout(out))
        code = main.main(args)
    return code, out.getvalue()


class CommandLineUsesThePipelineTests(unittest.TestCase):
    def test_cli_runs_through_run_pipeline_with_one_planner_agent(self):
        original = ManagerAgent.run_pipeline
        with mock.patch.object(ManagerAgent, "run_pipeline", autospec=True, side_effect=original) as spy:
            code, _ = run_cli(["A command-line calculator"], planner=RuleBasedPlanner())
        self.assertEqual(code, 0)
        self.assertEqual(spy.call_count, 1)
        agents = spy.call_args.args[2]
        self.assertEqual([type(agent).__name__ for agent in agents], ["_PlanOnlyAgent"])
        self.assertIsInstance(agents[0], PlannerAgent)

    def test_cli_output_is_exactly_what_it_was_before(self):
        code, text = run_cli(["  Build   a  website   with login "], planner=RuleBasedPlanner())
        lines = text.splitlines()
        self.assertEqual(code, 0)
        self.assertEqual(lines[:4], [
            "[Manager] Requirement received: Build a website with login",
            "[Manager] Detected features: authentication, web_ui",
            "[Manager] Handing off to RuleBasedPlanner...",
            "[Manager] Received a plan with 14 steps from RuleBasedPlanner.",
        ])
        self.assertEqual(lines[4:8], ["", "=" * 60, "DEVELOPMENT PLAN (by RuleBasedPlanner)",
                                      "Requirement: Build a website with login"])
        self.assertNotIn("Planner: done", text)  # the pipeline's own line is not added to the CLI output

    def test_cli_with_no_features_says_core_steps_only(self):
        _, text = run_cli(["A command-line calculator"], planner=RuleBasedPlanner())
        self.assertIn("[Manager] Detected features: none (core steps only)", text)

    def test_cli_asks_for_the_requirement_when_none_is_given(self):
        code, text = run_cli([], planner=RuleBasedPlanner(), typed="A REST API with a database")
        self.assertEqual(code, 0)
        self.assertIn("[Manager] Detected features: database, api", text)

    def test_cli_rejects_an_empty_requirement_without_running_a_planner(self):
        planner = CountingPlanner()
        code, text = run_cli(["   "], planner=planner)
        self.assertEqual(code, 1)
        self.assertEqual(text, "Error: The requirement is empty. Please describe the software you want.\n")
        self.assertEqual(planner.calls, 0)

    def test_cli_reports_the_ai_planner_fallback_as_before(self):
        failing = OllamaPlanner(send=lambda *args: (_ for _ in ()).throw(urllib.error.URLError("refused")))
        code, text = run_cli(["A command-line calculator"], planner=FallbackPlanner(failing, RuleBasedPlanner()))
        lines = text.splitlines()
        self.assertEqual(code, 0)
        self.assertEqual(lines[0], "[AETHER] Experimental local AI planner enabled. This can take a few minutes.")
        self.assertIn("[Manager] Handing off to OllamaPlanner...", lines)
        self.assertIn("[Manager] Received a plan with 8 steps from RuleBasedPlanner.", lines)
        self.assertIn("[AETHER] Local AI planner failed: Could not reach Ollama at http://127.0.0.1:11434 "
                      "(is it running?)", lines)
        self.assertIn("[AETHER] Used RuleBasedPlanner instead.", lines)

    def test_cli_asks_the_planner_exactly_once(self):
        planner = CountingPlanner()
        run_cli(["A website"], planner=planner)
        self.assertEqual(planner.calls, 1)


class RunWrapperCompatibilityTests(unittest.TestCase):
    def test_run_returns_the_plan_the_pipeline_produces(self):
        plan = manager().run(REQUIREMENT)
        context = manager().run_pipeline(REQUIREMENT, [PlannerAgent(RuleBasedPlanner())])
        self.assertEqual(plan, context.plan)

    def test_run_logs_the_same_four_messages_as_before(self):
        messages = []
        ManagerAgent(planner=RuleBasedPlanner(), log=messages.append).run(REQUIREMENT)
        self.assertEqual(messages, [
            f"[Manager] Requirement received: {REQUIREMENT}",
            "[Manager] Detected features: authentication, database, web_ui",
            "[Manager] Handing off to RuleBasedPlanner...",
            "[Manager] Received a plan with 18 steps from RuleBasedPlanner.",
        ])

    def test_run_calls_the_planner_once_with_the_cleaned_brief(self):
        seen = []

        class Spy(Planner):
            name = "Spy"

            def create_plan(self, brief):
                seen.append(brief)
                return Plan(brief.requirement, self.name, [PlanStep("Requirements", "a step here")])

        plan = ManagerAgent(planner=Spy(), log=lambda m: None).run("Build a website   with login")
        self.assertEqual(seen, [TaskBrief("Build a website with login", ["authentication", "web_ui"])])
        self.assertEqual(plan.planner_name, "Spy")

    def test_run_raises_value_error_for_an_unusable_requirement(self):
        messages = []
        with self.assertRaisesRegex(ValueError, "The requirement is empty"):
            ManagerAgent(planner=RuleBasedPlanner(), log=messages.append).run("   ")
        self.assertEqual(messages, [])  # nothing was logged, as before

    def test_run_raises_the_planner_exception_unchanged(self):
        class Broken(Planner):
            name = "Broken"

            def create_plan(self, brief):
                raise KeyError("planner exploded")

        with self.assertRaises(KeyError) as caught:
            ManagerAgent(planner=Broken(), log=lambda m: None).run(REQUIREMENT)
        self.assertEqual(caught.exception.args, ("planner exploded",))

    def test_run_restores_the_log_function_even_after_a_failure(self):
        log = lambda message: None  # noqa: E731
        failing = ManagerAgent(planner=RuleBasedPlanner(), log=log)
        with self.assertRaises(ValueError):
            failing.run("")
        self.assertIs(failing.log, log)
        failing.run(REQUIREMENT)
        self.assertIs(failing.log, log)

    def test_run_does_not_save_anything(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict("os.environ", {"AETHER_RUNS_DIR": folder}):
            manager().run(REQUIREMENT)
            self.assertEqual(list(Path(folder).iterdir()), [])


class SequentialExecutionTests(unittest.TestCase):
    def test_agents_run_once_each_in_list_order(self):
        order = []

        class Ordered(StepAgent):
            def run(self, context):
                order.append(self.name)
                return super().run(context)

        agents = [Ordered("First"), Ordered("Second"), Ordered("Third")]
        context = manager().run_pipeline(REQUIREMENT, agents)
        self.assertEqual(order, ["First", "Second", "Third"])
        self.assertEqual([agent.runs for agent in agents], [1, 1, 1])
        self.assertEqual(context.status, COMPLETED)
        self.assertEqual([(e.agent, e.status) for e in context.events],
                         [("Manager", DONE), ("First", DONE), ("Second", DONE), ("Third", DONE)])

    def test_planner_runs_once_and_later_agents_see_its_plan(self):
        planner = CountingPlanner()
        later = StepAgent("Later")
        context = manager().run_pipeline(REQUIREMENT, [PlannerAgent(planner), later])
        self.assertEqual(planner.calls, 1)
        self.assertEqual(later.runs, 1)
        self.assertEqual(len(context.plan.steps), 18)


class ApprovalAndResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RunStore(Path(self.temp.name))
        self.planner = CountingPlanner()
        self.gate = StepAgent("Builder", checkpoint="A")
        self.after = StepAgent("Reviewer")

    def agents(self):
        return [PlannerAgent(self.planner), self.gate, self.after]

    def pause(self):
        return manager().run_pipeline(REQUIREMENT, self.agents(), store=self.store)

    def test_run_pauses_at_the_checkpoint_before_the_agent_that_needs_approval(self):
        context = self.pause()
        self.assertEqual(context.status, AWAITING_APPROVAL)
        self.assertEqual((self.planner.calls, self.gate.runs, self.after.runs), (1, 0, 0))
        self.assertEqual(context.events[-1].status, WAITING)
        self.assertEqual(context.events[-1].detail, "Builder will run")

    def test_approving_and_resuming_runs_only_the_remaining_agents(self):
        mgr, context = manager(), self.pause()
        mgr.approve(context, "A", True, store=self.store)
        mgr.resume(context, self.agents(), self.store)
        self.assertEqual(context.status, COMPLETED)
        self.assertEqual((self.planner.calls, self.gate.runs, self.after.runs), (1, 1, 1))  # planner NOT run again
        done = [(e.agent, e.status) for e in context.events if e.status == DONE]
        self.assertEqual(done, [("Manager", DONE), ("Planner", DONE), ("User", DONE), ("Builder", DONE),
                                ("Reviewer", DONE)])

    def test_resume_without_approval_is_refused_and_runs_nothing(self):
        mgr, context = manager(), self.pause()
        context.status = COMPLETED  # not waiting any more
        with self.assertRaises(ValueError):
            mgr.resume(context, self.agents(), self.store)
        self.assertEqual((self.gate.runs, self.after.runs), (0, 0))

    def test_approve_requires_a_run_that_is_waiting(self):
        mgr, context = manager(), self.pause()
        mgr.approve(context, "A", True, store=self.store)
        mgr.resume(context, self.agents(), self.store)
        with self.assertRaises(ValueError):
            mgr.approve(context, "A", True, store=self.store)

    def test_a_completed_run_cannot_be_resumed_to_run_agents_twice(self):
        mgr, context = manager(), self.pause()
        mgr.approve(context, "A", True, store=self.store)
        mgr.resume(context, self.agents(), self.store)
        with self.assertRaises(ValueError):
            mgr.resume(context, self.agents(), self.store)
        self.assertEqual((self.planner.calls, self.gate.runs, self.after.runs), (1, 1, 1))

    def test_resume_after_restart_uses_only_the_saved_record_and_does_not_rerun_finished_agents(self):
        context = self.pause()
        reloaded = self.store.load(context.run_id)  # as the web app does when the page is reloaded
        fresh_planner, fresh_gate, fresh_after = CountingPlanner(), StepAgent("Builder", checkpoint="A"), StepAgent("Reviewer")
        mgr = manager()
        mgr.approve(reloaded, "A", True, store=self.store)
        mgr.resume(reloaded, [PlannerAgent(fresh_planner), fresh_gate, fresh_after], self.store)
        self.assertEqual(reloaded.status, COMPLETED)
        self.assertEqual((fresh_planner.calls, fresh_gate.runs, fresh_after.runs), (0, 1, 1))
        self.assertEqual(len([e for e in reloaded.events if e.agent == "Planner" and e.status == DONE]), 1)


class RejectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RunStore(Path(self.temp.name))
        self.gate, self.after = StepAgent("Builder", checkpoint="A"), StepAgent("Reviewer")
        self.agents = [PlannerAgent(RuleBasedPlanner()), self.gate, self.after]

    def test_rejection_cancels_the_run_and_skips_the_remaining_agents(self):
        mgr = manager()
        context = mgr.run_pipeline(REQUIREMENT, self.agents, store=self.store)
        mgr.approve(context, "A", False, note="not what I wanted", store=self.store, choice="ignored")
        mgr.resume(context, self.agents, self.store)

        self.assertEqual(context.status, CANCELLED)
        self.assertEqual((self.gate.runs, self.after.runs), (0, 0))
        self.assertEqual(context.approvals[-1].decision, "rejected")
        self.assertEqual(context.approvals[-1].choice, "")  # a choice is only kept when approving
        tail = [(e.agent, e.status) for e in context.events[-3:]]
        self.assertEqual(tail, [("User", FAILED), ("Builder", SKIPPED), ("Reviewer", SKIPPED)])
        self.assertIsNotNone(context.plan)  # the plan made before the rejection is kept

    def test_resuming_a_cancelled_run_again_adds_no_duplicate_events(self):
        mgr = manager()
        context = mgr.run_pipeline(REQUIREMENT, self.agents, store=self.store)
        mgr.approve(context, "A", False, store=self.store)
        mgr.resume(context, self.agents, self.store)
        count = len(context.events)
        mgr.resume(context, self.agents, self.store)
        self.assertEqual(len(context.events), count)
        self.assertEqual(context.status, CANCELLED)


class FailureHandlingTests(unittest.TestCase):
    def test_a_crashing_agent_fails_the_run_and_later_agents_never_run(self):
        later = [StepAgent("Reviewer"), StepAgent("Tester")]
        agents = [PlannerAgent(RuleBasedPlanner()), StepAgent("Builder", error=RuntimeError("disk full"))] + later
        context = manager().run_pipeline(REQUIREMENT, agents)
        self.assertEqual(context.status, FAILED)
        self.assertEqual([agent.runs for agent in later], [0, 0])
        self.assertEqual([(e.agent, e.status) for e in context.events[-3:]],
                         [("Builder", FAILED), ("Reviewer", SKIPPED), ("Tester", SKIPPED)])
        self.assertIn("RuntimeError: disk full", context.events[-3].detail)

    def test_an_agent_reporting_failure_also_stops_the_run(self):
        later = StepAgent("Reviewer")
        context = manager().run_pipeline(REQUIREMENT, [StepAgent("Builder", status=FAILED), later])
        self.assertEqual(context.status, FAILED)
        self.assertEqual(later.runs, 0)
        self.assertEqual(context.events[-1].status, SKIPPED)

    def test_failure_after_a_checkpoint_resume_skips_the_rest(self):
        mgr = manager()
        gate, after = StepAgent("Builder", error=ValueError("bad template"), checkpoint="A"), StepAgent("Reviewer")
        agents = [PlannerAgent(RuleBasedPlanner()), gate, after]
        context = mgr.run_pipeline(REQUIREMENT, agents)
        mgr.approve(context, "A", True)
        mgr.resume(context, agents)
        self.assertEqual(context.status, FAILED)
        self.assertEqual((gate.runs, after.runs), (1, 0))
        self.assertEqual(context.events[-1].status, SKIPPED)

    def test_an_empty_requirement_fails_the_run_before_any_agent(self):
        agent = StepAgent("Builder")
        context = manager().run_pipeline("  ", [agent])
        self.assertEqual((context.status, agent.runs), (FAILED, 0))


class EventPersistenceTests(unittest.TestCase):
    def test_events_are_saved_in_both_files_through_pause_approval_and_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            store = RunStore(Path(folder))
            gate = StepAgent("Builder", checkpoint="A")
            agents = [PlannerAgent(RuleBasedPlanner()), gate]
            mgr = manager()

            context = mgr.run_pipeline(REQUIREMENT, agents, store=store)
            self.assertEqual(store.load(context.run_id), context)  # saved at the pause
            self.assertEqual(store.load_events(context.run_id), context.events)
            paused = len(context.events)

            mgr.approve(context, "A", True, store=store)
            mgr.resume(context, agents, store)
            self.assertEqual(store.load(context.run_id), context)  # saved after completion
            self.assertEqual(store.load_events(context.run_id), context.events)
            self.assertGreater(len(context.events), paused)
            self.assertEqual(store.load(context.run_id).approvals[0].decision, "approved")

    def test_events_are_saved_when_the_run_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            store = RunStore(Path(folder))
            context = manager().run_pipeline(REQUIREMENT, [StepAgent("Builder", error=RuntimeError("x"))], store=store)
            self.assertEqual(store.load_events(context.run_id)[-1].status, FAILED)
            self.assertEqual(store.load(context.run_id).status, FAILED)

    def test_without_a_store_nothing_is_written(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict("os.environ", {"AETHER_RUNS_DIR": folder}):
            manager().run_pipeline(REQUIREMENT, [PlannerAgent(RuleBasedPlanner())])
            self.assertEqual(list(Path(folder).iterdir()), [])


class SingleImplementationTests(unittest.TestCase):
    """The structure itself: there is one place that runs agents, and nothing else plans or runs them."""

    @staticmethod
    def calls(path: str, attr: str) -> list:
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        return [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == attr]

    def test_the_manager_runs_agents_in_exactly_one_place(self):
        runs = [call for call in self.calls("aether/manager.py", "run")
                if isinstance(call.func.value, ast.Name) and call.func.value.id == "agent"]
        self.assertEqual(len(runs), 1)

    def test_the_manager_never_calls_a_planner_directly(self):
        self.assertEqual(self.calls("aether/manager.py", "create_plan"), [])

    def test_the_command_line_never_calls_a_planner_or_the_agents_directly(self):
        self.assertEqual(self.calls("main.py", "create_plan"), [])
        self.assertEqual([c for c in self.calls("main.py", "run")
                          if getattr(c.func.value, "id", "") == "agent"], [])

    def test_run_is_a_thin_wrapper_that_delegates_to_run_pipeline(self):
        tree = ast.parse((ROOT / "aether" / "manager.py").read_text(encoding="utf-8"))
        run = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "run")
        called = {call.func.attr for call in ast.walk(run) if isinstance(call, ast.Call)
                  and isinstance(call.func, ast.Attribute)}
        self.assertIn("run_pipeline", called)
        self.assertLessEqual(run.end_lineno - run.lineno, 25)

    def test_the_web_workflow_still_uses_run_pipeline_and_resume(self):
        source = (ROOT / "aether_ui" / "workflow.py").read_text(encoding="utf-8")
        self.assertIn("manager.run_pipeline(", source)
        self.assertIn("manager.resume(", source)


if __name__ == "__main__":
    unittest.main()
