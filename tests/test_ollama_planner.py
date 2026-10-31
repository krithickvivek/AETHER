"""Tests for OllamaPlanner, FallbackPlanner and planner configuration.

All model responses are mocked. urllib.request.urlopen is patched to fail the
test if anything tries to make a real network call, so Ollama is never needed.
"""

import json
import os
import unittest
import urllib.error
from unittest import mock

from aether.config import build_planner, planner_setting
from aether.fallback import FallbackPlanner
from aether.models import TaskBrief
from aether.ollama_planner import (
    OllamaPlanner, PlannerError, build_prompt, ollama_status, parse_and_validate, project_tier,
)
from aether.planner import PHASES, Planner, RuleBasedPlanner
from aether_ui.workflow import DONE, FAILED, result_from_dict, result_to_dict, run_workflow, step_origin

GOOD_STEPS = [
    ("Requirements", "Agree the core to-do features and who the users are"),
    ("Design", "Design the login and registration flow with hashed passwords"),
    ("Design", "Design the database schema for users and tasks"),
    ("Design", "Sketch the main web pages for the task list"),
    ("Setup", "Create the repository and choose a web framework"),
    ("Implementation", "Implement user registration, login and logout"),
    ("Implementation", "Implement saving and loading tasks in the database"),
    ("Implementation", "Build the task list page and the add-task form"),
    ("Implementation", "Add input validation and clear error messages to the forms"),
    ("Testing", "Test login with valid and invalid passwords"),
    ("Testing", "Test that tasks are stored and reloaded correctly"),
    ("Documentation", "Write a README with setup and usage instructions"),
]
FEATURES = ["authentication", "database", "web_ui"]


def chat_response(steps, **extra) -> dict:
    content = json.dumps({"steps": [{"phase": p, "description": d} for p, d in steps]})
    return {"message": {"role": "assistant", "content": content}, "eval_count": 300,
            "total_duration": 90_000_000_000, **extra}


def fake_send(response=None, error: Exception | None = None):
    """A stand-in for the HTTP call. Records the payload it was given."""
    calls = []

    def send(url, payload, timeout):
        calls.append({"url": url, "payload": payload, "timeout": timeout})
        if error:
            raise error
        return response

    send.calls = calls
    return send


def no_network(*args, **kwargs):
    raise AssertionError("A test tried to make a real network call.")


class NoNetworkTestCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("urllib.request.urlopen", side_effect=no_network)
        patcher.start()
        self.addCleanup(patcher.stop)


class OllamaPlannerTests(NoNetworkTestCase):
    def brief(self, features=FEATURES):
        return TaskBrief("Build a to-do website where users can log in and save tasks", list(features))

    def test_valid_answer_becomes_ordered_plan(self):
        shuffled = GOOD_STEPS[::-1]  # the model may answer out of order
        planner = OllamaPlanner(send=fake_send(chat_response(shuffled)))
        plan = planner.create_plan(self.brief())

        self.assertEqual(plan.planner_name, "OllamaPlanner")
        self.assertEqual(len(plan.steps), len(GOOD_STEPS))
        positions = [PHASES.index(s.phase) for s in plan.steps]
        self.assertEqual(positions, sorted(positions))

    def test_request_is_local_small_and_unloads_model(self):
        send = fake_send(chat_response(GOOD_STEPS))
        OllamaPlanner(send=send).create_plan(self.brief())

        call = send.calls[0]
        self.assertTrue(call["url"].startswith("http://127.0.0.1:11434/"))
        self.assertEqual(call["payload"]["model"], "llama3.2:1b")
        self.assertEqual(call["payload"]["keep_alive"], 0)
        self.assertEqual(call["payload"]["options"]["num_ctx"], 2048)
        self.assertIn("format", call["payload"])

    def test_prompt_asks_for_each_detected_feature(self):
        prompt = build_prompt(self.brief())
        self.assertIn("user registration, login and logout", prompt)
        self.assertIn("storing and loading data", prompt)

    def test_rejects_invalid_json(self):
        planner = OllamaPlanner(send=fake_send({"message": {"content": "Sure! Here is a plan:"}}))
        with self.assertRaisesRegex(PlannerError, "expected JSON"):
            planner.create_plan(self.brief())

    def test_drops_unknown_phases_and_empty_descriptions(self):
        steps = GOOD_STEPS + [("Deployment", "Deploy to production servers"), ("Testing", "   ")]
        plan = OllamaPlanner(send=fake_send(chat_response(steps))).create_plan(self.brief())
        self.assertEqual(len(plan.steps), len(GOOD_STEPS))
        self.assertTrue(all(s.phase in PHASES and s.description for s in plan.steps))

    def test_removes_duplicate_and_reworded_steps(self):
        steps = GOOD_STEPS + [
            ("Testing", "Test login with valid and invalid passwords"),
            ("Testing", "Test login with valid and invalid password"),
        ]
        plan = OllamaPlanner(send=fake_send(chat_response(steps))).create_plan(self.brief())
        self.assertEqual(len(plan.steps), len(GOOD_STEPS))

    def test_rejects_plan_missing_a_detected_feature(self):
        without_login = [s for s in GOOD_STEPS if "login" not in s[1].lower() and "password" not in s[1].lower()]
        planner = OllamaPlanner(send=fake_send(chat_response(without_login)))
        with self.assertRaisesRegex(PlannerError, "does not cover: authentication"):
            planner.create_plan(self.brief())

    def test_rejects_too_few_steps(self):
        planner = OllamaPlanner(send=fake_send(chat_response(GOOD_STEPS[:3])))
        with self.assertRaisesRegex(PlannerError, "usable steps"):
            planner.create_plan(self.brief(features=[]))

    def test_rejects_repetitive_plan_like_the_real_1b_test(self):
        # Shape of the real llama3.2:1b output measured on this laptop: mostly Testing/Documentation.
        steps = [("Requirements", "Define the requirements"), ("Design", "Create the design")]
        steps += [("Testing", f"Perform testing round number {i} for stability {'x' * i}") for i in range(7)]
        steps += [("Documentation", f"Write document number {i} about maintenance {'y' * i}") for i in range(5)]
        planner = OllamaPlanner(send=fake_send(chat_response(steps)))
        with self.assertRaises(PlannerError):
            planner.create_plan(self.brief(features=[]))

    def test_unreachable_server_becomes_planner_error(self):
        for error in (urllib.error.URLError("connection refused"), ConnectionRefusedError("refused")):
            planner = OllamaPlanner(send=fake_send(error=error))
            with self.assertRaisesRegex(PlannerError, "Could not reach Ollama"):
                planner.create_plan(self.brief())

    def test_timeout_says_the_model_was_too_slow_not_unreachable(self):
        for error in (TimeoutError("timed out"), urllib.error.URLError(TimeoutError("timed out"))):
            planner = OllamaPlanner(send=fake_send(error=error))
            with self.assertRaises(PlannerError) as caught:
                planner.create_plan(self.brief())
            self.assertIn("took longer than 5 minutes", str(caught.exception))
            self.assertNotIn("Could not reach", str(caught.exception))

    def test_output_is_capped_and_cut_off_answer_is_rejected(self):
        send = fake_send(chat_response(GOOD_STEPS, done_reason="length"))
        with self.assertRaisesRegex(PlannerError, "cut off at 900 tokens"):
            OllamaPlanner(send=send).create_plan(self.brief())
        self.assertEqual(send.calls[0]["payload"]["options"]["num_predict"], 900)

    def test_complete_answer_is_not_treated_as_cut_off(self):
        plan = OllamaPlanner(send=fake_send(chat_response(GOOD_STEPS, done_reason="stop"))).create_plan(self.brief())
        self.assertEqual(plan.planner_name, "OllamaPlanner")

    def test_missing_model_becomes_planner_error(self):
        error = urllib.error.HTTPError("http://127.0.0.1:11434/api/chat", 404, "Not Found", {}, None)
        planner = OllamaPlanner(send=fake_send(error=error))
        with self.assertRaisesRegex(PlannerError, "HTTP 404"):
            planner.create_plan(self.brief())

    def test_only_local_server_and_approved_model_allowed(self):
        with self.assertRaises(ValueError):
            OllamaPlanner(base_url="https://api.example.com")
        with self.assertRaises(ValueError):
            OllamaPlanner(model="llama3.2:3b")
        with self.assertRaises(ValueError):
            OllamaPlanner(model="gpt-oss:120b-cloud")

    def test_status_check_reports_server_down_without_raising(self):
        refused = urllib.error.URLError("connection refused")
        with mock.patch("urllib.request.urlopen", side_effect=refused):
            ready, message = ollama_status()
        self.assertFalse(ready)
        self.assertIn("not running", message)


# The real llama3.2:1b answer for the Library REST API example, from the browser test on 30 Oct 2026.
REAL_LIBRARY_STEPS = [
    ("Requirements", "Define the library's functionality and data structure requirements through user stories and acceptance criteria."),
    ("Design", "Create a high-level design for the REST API, including the database schema and API endpoints."),
    ("Setup", "Set up the development environment, including the database and API server."),
    ("Implementation", "Implement the REST API using a programming language and framework, following the design specifications."),
    ("Testing", "Write unit tests and integration tests for the API to ensure it meets the requirements and is stable."),
    ("Testing", "Perform unit and integration testing to ensure the API is working as expected."),
    ("Testing", "Perform end-to-end testing to ensure the API is working as expected and is stable."),
    ("Testing", "Perform security testing to ensure the API is secure and protected against common web vulnerabilities."),
    ("Testing", "Perform performance testing to ensure the API is working efficiently and can handle a large number of requests."),
    ("Testing", "Perform load testing to ensure the API can handle a large number of concurrent requests."),
    ("Documentation", "Create documentation for the API, including API references, error handling, and usage examples."),
    ("Documentation", "Create user documentation for the API, including guides, tutorials, and API reference."),
    ("Documentation", "Create API security documentation, including security best practices and threat modeling."),
    ("Documentation", "Create API performance documentation, including performance metrics and optimization recommendations."),
]
LIBRARY_FEATURES = ["database", "api"]

BALANCED_LIBRARY_STEPS = [
    ("Requirements", "List the books, members and loans the library needs to track"),
    ("Design", "Design the database schema for books, members and loans"),
    ("Design", "Design the REST API endpoints and their request and response formats"),
    ("Setup", "Set up the project, the web framework and the database connection"),
    ("Implementation", "Implement saving and loading books and members in the database"),
    ("Implementation", "Build the REST API endpoints for books and members"),
    ("Implementation", "Add input validation and clear error responses to every endpoint"),
    ("Testing", "Write unit tests for the database storage functions"),
    ("Testing", "Test every API endpoint with valid and invalid requests"),
    ("Documentation", "Document the API endpoints with request and response examples"),
]

CALCULATOR_STEPS = [
    ("Requirements", "List the arithmetic operations the calculator must support"),
    ("Design", "Design the command-line arguments and the output format"),
    ("Setup", "Create the project folder and a virtual environment"),
    ("Implementation", "Implement addition, subtraction, multiplication and division with input checks"),
    ("Testing", "Write unit tests for each arithmetic operation"),
    ("Testing", "Test division by zero and invalid number input"),
    ("Documentation", "Write a README with usage examples"),
]
TODO_REQUIREMENT = "Build a to-do website where users can log in and save tasks"


def validate(steps, features, requirement="A software project"):
    content = json.dumps({"steps": [{"phase": p, "description": d} for p, d in steps]})
    return parse_and_validate(content, TaskBrief(requirement, list(features)))


class ProjectTierTests(unittest.TestCase):
    def test_tiers_follow_feature_weights(self):
        self.assertEqual(project_tier([]).name, "Simple")
        self.assertEqual(project_tier(["web_ui"]).name, "Simple")
        self.assertEqual(project_tier(["database", "api"]).name, "Moderate")
        self.assertEqual(project_tier(FEATURES).name, "Complex")

    def test_prompt_states_the_tier_and_implementation_target(self):
        prompt = build_prompt(TaskBrief(TODO_REQUIREMENT, FEATURES))
        self.assertIn("complex project", prompt)
        self.assertIn("Implementation 5-7", prompt)  # minimum 4, asked for a little more


class SimpleRequirementTests(NoNetworkTestCase):
    def test_short_plan_with_one_implementation_step_passes(self):
        steps = validate(CALCULATOR_STEPS, [], "A command-line calculator for basic arithmetic")
        self.assertEqual(len(steps), len(CALCULATOR_STEPS))

    def test_three_steps_in_one_phase_are_allowed_in_a_small_plan(self):
        steps = CALCULATOR_STEPS + [("Testing", "Check that very large numbers are printed correctly")]
        self.assertEqual(len(validate(steps, [])), 8)

    def test_slightly_different_distribution_still_passes(self):
        # No Setup step and an extra Documentation step: off the suggested targets, but still useful.
        steps = [s for s in CALCULATOR_STEPS if s[0] != "Setup"]
        steps.append(("Documentation", "Add a help message that lists every supported operation"))
        self.assertEqual(len(validate(steps, [])), 7)


class ModerateRequirementTests(NoNetworkTestCase):
    def test_balanced_plan_passes(self):
        steps = validate(BALANCED_LIBRARY_STEPS, LIBRARY_FEATURES)
        self.assertEqual(len(steps), len(BALANCED_LIBRARY_STEPS))

    def test_real_unbalanced_1b_plan_is_rejected_with_every_reason(self):
        with self.assertRaises(PlannerError) as caught:
            validate(REAL_LIBRARY_STEPS, LIBRARY_FEATURES)
        message = str(caught.exception)
        self.assertTrue(message.startswith("Moderate project (database, api): "))
        self.assertIn("too few implementation steps (1, need 2)", message)
        self.assertIn("testing and documentation outweigh implementation (10 vs 1)", message)
        self.assertIn("no implementation step builds: database", message)


class ComplexRequirementTests(NoNetworkTestCase):
    def test_full_plan_passes(self):
        self.assertEqual(len(validate(GOOD_STEPS, FEATURES, TODO_REQUIREMENT)), len(GOOD_STEPS))

    def test_plan_that_suits_a_simple_project_is_too_thin(self):
        with self.assertRaises(PlannerError) as caught:
            validate(CALCULATOR_STEPS, FEATURES, TODO_REQUIREMENT)
        message = str(caught.exception)
        self.assertIn("Complex project", message)
        self.assertIn("usable steps (expected 10-30)", message)
        self.assertIn("too few implementation steps (1, need 4)", message)

    def test_more_testing_than_suggested_still_passes(self):
        steps = GOOD_STEPS + [
            ("Testing", "Check the pages on a phone-sized screen"),
            ("Testing", "Run a security check for weak passwords and session expiry"),
        ]
        self.assertEqual(len(validate(steps, FEATURES, TODO_REQUIREMENT)), len(steps))


class FeatureCoverageTests(NoNetworkTestCase):
    def replace(self, old_description, new_step):
        return [new_step if d == old_description else (p, d) for p, d in GOOD_STEPS]

    def test_database_mentioned_only_in_design_is_not_implemented(self):
        steps = self.replace("Implement saving and loading tasks in the database",
                             ("Implementation", "Implement editing and deleting tasks from the list"))
        with self.assertRaisesRegex(PlannerError, "no implementation step builds: database"):
            validate(steps, FEATURES, TODO_REQUIREMENT)

    def test_design_wording_inside_implementation_phase_does_not_count(self):
        steps = self.replace("Implement saving and loading tasks in the database",
                             ("Implementation", "Think about the database tables for tasks"))
        with self.assertRaisesRegex(PlannerError, "no implementation step builds: database"):
            validate(steps, FEATURES, TODO_REQUIREMENT)

    def test_feature_needs_more_than_one_mention(self):
        steps = [s for s in BALANCED_LIBRARY_STEPS if "database" not in s[1].lower() or s[0] == "Implementation"]
        steps = [s for s in steps if "storage" not in s[1].lower()]
        steps += [("Testing", "Test that books cannot be borrowed twice at once"),
                  ("Design", "Decide the rules for borrowing and returning books")]
        with self.assertRaisesRegex(PlannerError, "only one step mentions: database"):
            validate(steps, LIBRARY_FEATURES)


class DuplicateAndVagueStepTests(NoNetworkTestCase):
    def test_different_kinds_of_testing_are_kept(self):
        pairs = [
            ("Perform security testing to ensure the API is secure against common attacks",
             "Perform performance testing to ensure the API is secure against common attacks"),
            ("Perform unit and integration testing to ensure the API is working as expected.",
             "Perform end-to-end testing to ensure the API is working as expected and is stable."),
            ("Perform performance testing to ensure the API is working efficiently and can handle a large number of requests.",
             "Perform load testing to ensure the API can handle a large number of concurrent requests."),
        ]
        for first, second in pairs:
            with self.subTest(first=first):
                steps = validate(BALANCED_LIBRARY_STEPS + [("Testing", first), ("Testing", second)], LIBRARY_FEATURES)
                kept = [s.description for s in steps]
                self.assertIn(first, kept)
                self.assertIn(second, kept)

    def test_reworded_step_is_removed(self):
        steps = GOOD_STEPS + [("Testing", "Check that login works with valid and invalid passwords")]
        descriptions = [s.description for s in validate(steps, FEATURES, TODO_REQUIREMENT)]
        self.assertNotIn("Check that login works with valid and invalid passwords", descriptions)
        self.assertEqual(len(descriptions), len(GOOD_STEPS))

    def test_vague_steps_are_dropped(self):
        steps = GOOD_STEPS + [("Design", "Create the design"), ("Testing", "Write tests")]
        descriptions = [s.description for s in validate(steps, FEATURES, TODO_REQUIREMENT)]
        self.assertNotIn("Create the design", descriptions)
        self.assertNotIn("Write tests", descriptions)

    def test_too_many_dropped_steps_means_a_repetitive_plan(self):
        rewordings = [
            ("Testing", "Check that login works with valid and invalid passwords"),
            ("Testing", "Test the login with valid and also invalid passwords"),
            ("Design", "Design the database schema for the users and the tasks"),
            ("Design", "Create the design"),
            ("Testing", "Write tests"),
        ]
        with self.assertRaisesRegex(PlannerError, "5 of 17 steps were vague, unusable or repeated"):
            validate(GOOD_STEPS + rewordings, FEATURES, TODO_REQUIREMENT)


class FailingPlanner(Planner):
    name = "FailingPlanner"

    def create_plan(self, brief):
        raise PlannerError("model output was not usable")


class FallbackPlannerTests(NoNetworkTestCase):
    def test_uses_fallback_and_records_reason(self):
        planner = FallbackPlanner(FailingPlanner(), RuleBasedPlanner())
        plan = planner.create_plan(TaskBrief("A website", ["web_ui"]))
        self.assertEqual(plan.planner_name, "RuleBasedPlanner")
        self.assertEqual(planner.fallback_reason, "model output was not usable")

    def test_no_reason_when_primary_succeeds(self):
        primary = OllamaPlanner(send=fake_send(chat_response(GOOD_STEPS)))
        planner = FallbackPlanner(primary, RuleBasedPlanner())
        plan = planner.create_plan(TaskBrief("To-do site with login", FEATURES))
        self.assertEqual(plan.planner_name, "OllamaPlanner")
        self.assertIsNone(planner.fallback_reason)


class ConfigTests(unittest.TestCase):
    def test_rule_based_is_the_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AETHER_PLANNER", None)
            self.assertEqual(planner_setting(), "rule")
            self.assertIsInstance(build_planner(), RuleBasedPlanner)

    def test_ollama_setting_builds_fallback_planner(self):
        with mock.patch.dict(os.environ, {"AETHER_PLANNER": "ollama"}):
            planner = build_planner()
        self.assertIsInstance(planner, FallbackPlanner)
        self.assertIsInstance(planner.primary, OllamaPlanner)
        self.assertIsInstance(planner.fallback, RuleBasedPlanner)

    def test_unknown_setting_is_rejected(self):
        with mock.patch.dict(os.environ, {"AETHER_PLANNER": "gpt"}):
            with self.assertRaises(ValueError):
                planner_setting()


class WorkflowWithAIPlannerTests(NoNetworkTestCase):
    REQUIREMENT = "Build a to-do website where users can log in and save tasks"

    def test_fallback_is_shown_as_failed_stage_then_rule_based_plan(self):
        primary = OllamaPlanner(send=fake_send(error=urllib.error.URLError("refused")))
        result = run_workflow(self.REQUIREMENT, planner=FallbackPlanner(primary, RuleBasedPlanner()))

        self.assertTrue(result.ok)
        self.assertFalse(result.ai_generated)
        self.assertEqual(result.plan.planner_name, "RuleBasedPlanner")
        self.assertIn("Could not reach Ollama", result.fallback_reason)
        self.assertEqual([s.status for s in result.stages], [DONE, DONE, FAILED, DONE])
        self.assertEqual(result.stages[-1].agent, "RuleBasedPlanner")

    def test_ai_plan_is_labelled_as_ai(self):
        primary = OllamaPlanner(send=fake_send(chat_response(GOOD_STEPS)))
        result = run_workflow(self.REQUIREMENT, planner=FallbackPlanner(primary, RuleBasedPlanner()))

        self.assertTrue(result.ai_generated)
        self.assertIsNone(result.fallback_reason)
        self.assertEqual([s.status for s in result.stages], [DONE, DONE, DONE])
        self.assertEqual(result.stages[-1].agent, "OllamaPlanner")
        self.assertIsNone(step_origin(result.plan.steps[0], result.plan.planner_name))

    def test_fallback_reason_survives_history_round_trip(self):
        primary = OllamaPlanner(send=fake_send(error=TimeoutError("timed out")))
        result = run_workflow(self.REQUIREMENT, planner=FallbackPlanner(primary, RuleBasedPlanner()))
        restored = result_from_dict(result_to_dict(result))
        self.assertEqual(restored.fallback_reason, result.fallback_reason)

    def test_quality_failure_falls_back_and_shows_the_reason(self):
        primary = OllamaPlanner(send=fake_send(chat_response(REAL_LIBRARY_STEPS)))
        requirement = "Create a REST API for a library that stores books and members in a database"
        result = run_workflow(requirement, planner=FallbackPlanner(primary, RuleBasedPlanner()))

        self.assertEqual(result.plan.planner_name, "RuleBasedPlanner")
        self.assertEqual([s.status for s in result.stages], [DONE, DONE, FAILED, DONE])
        self.assertIn("too few implementation steps", result.stages[2].detail)
        self.assertIn("too few implementation steps", result.fallback_reason)

    def test_older_history_entries_still_load(self):
        data = result_to_dict(run_workflow(self.REQUIREMENT))
        data.pop("fallback_reason")
        self.assertIsNone(result_from_dict(data).fallback_reason)


if __name__ == "__main__":
    unittest.main()
