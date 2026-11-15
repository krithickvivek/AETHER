"""Tests for conservative template selection and the Checkpoint A override.

Includes regression tests for the wrong selections found in the technical audit.
Files are generated into temporary folders only. Generated code is never run.
"""

import tempfile
import unittest
from pathlib import Path

from aether.agents import PlannerAgent
from aether.context import APPROVED, COMPLETED, FAILED, ProjectContext, RunStore
from aether.developer import DeveloperAgent, approved_override, preview
from aether.manager import ManagerAgent
from aether.planner import RuleBasedPlanner
from aether.templates import (
    AMBIGUOUS, NO_MATCH, OVERRIDDEN, POOR_FIT, SELECTED, TEMPLATES, choose_template, evaluate_fit,
)

# Requirements the old selection got wrong (see the technical audit).
AUDIT_REGRESSIONS = {
    "hotel booking": "Book a hotel room online with a web page",
    "tax dashboard": "An app that calculates monthly tax and saves records in a database with a web dashboard",
    "task API": "A REST API to manage tasks",
}
VALID = {
    "calculator": [
        "A command-line calculator for basic arithmetic",
        "Make a simple calculator",
        "A calc tool for arithmetic in the terminal",
    ],
    "todo_web": [
        "Build a to-do website where users can log in and save their tasks",
        "A todo list web app",
        "A task manager website with user accounts",
    ],
    "library_api": [
        "Create a REST API for a library that stores books and members in a database",
        "A library management system API for books and loans",
    ],
}


def brief(requirement):
    return ManagerAgent(RuleBasedPlanner(), log=lambda m: None).understand(requirement)


def choose(requirement, override=None):
    return choose_template(brief(requirement), override)


class AuditRegressionTests(unittest.TestCase):
    def test_hotel_booking_does_not_select_the_library_api(self):
        choice = choose(AUDIT_REGRESSIONS["hotel booking"])
        self.assertIsNone(choice.template)
        self.assertEqual(choice.status, NO_MATCH)
        self.assertIn("book", choice.hints)  # "book" is only a related word, never evidence
        self.assertFalse(choice.can_override)

    def test_tax_dashboard_does_not_select_the_calculator(self):
        choice = choose(AUDIT_REGRESSIONS["tax dashboard"])
        self.assertIsNone(choice.template)
        self.assertEqual(choice.status, NO_MATCH)
        self.assertEqual(choice.unsupported, ["database", "web_ui"])
        self.assertIn("calculates", choice.hints)
        self.assertFalse(choice.can_override)

    def test_task_api_does_not_select_the_todo_web_app(self):
        choice = choose(AUDIT_REGRESSIONS["task API"])
        self.assertIsNone(choice.template)
        self.assertEqual(choice.status, NO_MATCH)
        self.assertEqual(choice.unsupported, ["api"])
        self.assertIn("tasks", choice.hints)
        self.assertFalse(choice.can_override)

    def test_reason_explains_related_words_are_not_enough(self):
        reason = choose(AUDIT_REGRESSIONS["hotel booking"]).reason
        self.assertIn("No supported project type", reason)
        self.assertIn('"book"', reason)
        self.assertIn("do not identify a project type on their own", reason)


class ValidSelectionTests(unittest.TestCase):
    def test_clear_requirements_select_their_template(self):
        for key, requirements in VALID.items():
            for requirement in requirements:
                with self.subTest(requirement=requirement):
                    choice = choose(requirement)
                    self.assertEqual(choice.status, SELECTED)
                    self.assertEqual(choice.template.key, key)
                    self.assertTrue(choice.evidence)
                    self.assertEqual(choice.candidates, [])

    def test_selected_examples_report_supported_requirements(self):
        self.assertEqual(choose(VALID["todo_web"][0]).supported, ["authentication", "database", "web_ui"])
        self.assertEqual(choose(VALID["library_api"][0]).supported, ["api", "database"])
        self.assertEqual(choose(VALID["calculator"][0]).supported, [])

    def test_one_minor_unsupported_feature_still_selects_but_is_reported(self):
        choice = choose("A to-do website with login and a public API")
        self.assertEqual(choice.status, SELECTED)
        self.assertEqual(choice.supported, ["authentication", "web_ui"])
        self.assertEqual(choice.unsupported, ["api"])


class ConservativeSelectionTests(unittest.TestCase):
    def test_generic_words_never_select_a_template(self):
        for requirement in ["A website where users create an account and save records",
                            "Build an online store with a database",
                            "A web page to do online shopping",
                            "Track my book reading habits"]:
            with self.subTest(requirement=requirement):
                self.assertIsNone(choose(requirement).template)
                self.assertEqual(choose(requirement).status, NO_MATCH)

    def test_two_project_types_are_reported_as_ambiguous(self):
        choice = choose("A to-do list for library staff")
        self.assertIsNone(choice.template)
        self.assertEqual(choice.status, AMBIGUOUS)
        self.assertEqual({t.key for t in choice.candidates}, {"todo_web", "library_api"})
        self.assertIn("will not guess", choice.reason)

    def test_mostly_unsupported_features_reject_automatic_selection(self):
        choice = choose("A calculator web page with user accounts")
        self.assertIsNone(choice.template)
        self.assertEqual(choice.status, POOR_FIT)
        self.assertEqual(choice.unsupported, ["authentication", "web_ui"])
        self.assertEqual([t.key for t in choice.candidates], ["calculator"])
        self.assertTrue(choice.can_override)

    def test_fit_evaluation_separates_evidence_hints_and_features(self):
        library = next(t for t in TEMPLATES if t.key == "library_api")
        fit = evaluate_fit(library, brief("A library API where members borrow books"))
        self.assertEqual(fit.evidence, ["library"])
        self.assertEqual(fit.hints, ["borrow", "books"])
        self.assertEqual(fit.supported, ["api"])
        self.assertTrue(fit.suitable)


class OverrideChoiceTests(unittest.TestCase):
    def test_override_uses_a_named_candidate_and_says_so(self):
        choice = choose("A calculator web page with user accounts", override="calculator")
        self.assertEqual(choice.status, OVERRIDDEN)
        self.assertEqual(choice.template.key, "calculator")
        self.assertEqual(choice.unsupported, ["authentication", "web_ui"])
        self.assertIn("Explicit override", choice.reason)

    def test_override_is_refused_for_a_project_type_that_was_not_named(self):
        for requirement, key in [("A calculator web page with user accounts", "library_api"),
                                 (AUDIT_REGRESSIONS["hotel booking"], "library_api"),
                                 (AUDIT_REGRESSIONS["task API"], "todo_web")]:
            with self.subTest(requirement=requirement):
                choice = choose(requirement, override=key)
                self.assertIsNone(choice.template)
                self.assertIn("not allowed", choice.reason)


class CheckpointOverrideTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RunStore(Path(self.temp.name))
        self.manager = ManagerAgent(RuleBasedPlanner(), log=lambda message: None)

    def agents(self):
        return [PlannerAgent(RuleBasedPlanner()), DeveloperAgent(self.store)]

    def run_to_checkpoint(self, requirement):
        return self.manager.run_pipeline(requirement, self.agents(), store=self.store)

    def decide(self, context, choice=""):
        self.manager.approve(context, "A", approved=True, store=self.store, choice=choice)
        return self.manager.resume(context, self.agents(), store=self.store)

    def assert_nothing_written(self, context):
        self.assertEqual(context.files, [])
        workspace = self.store.workspace_dir(context.run_id)
        self.assertFalse(workspace.exists() and any(workspace.iterdir()))

    def test_audit_regressions_generate_nothing_even_if_approved(self):
        for name, requirement in AUDIT_REGRESSIONS.items():
            with self.subTest(example=name):
                context = self.decide(self.run_to_checkpoint(requirement))
                self.assertEqual(context.status, FAILED)
                self.assertEqual(context.events[-1].agent, "Developer")
                self.assertIn("Unsupported requirement", context.events[-1].detail)
                self.assert_nothing_written(context)

    def test_poor_fit_without_override_generates_nothing(self):
        context = self.decide(self.run_to_checkpoint("A calculator web page with user accounts"))
        self.assertEqual(context.status, FAILED)
        self.assert_nothing_written(context)

    def test_explicit_override_is_recorded_and_builds_the_chosen_template(self):
        context = self.decide(self.run_to_checkpoint("A calculator web page with user accounts"), choice="calculator")

        self.assertEqual(context.status, COMPLETED)
        self.assertEqual(context.project_type, "calculator")
        self.assertEqual((context.approvals[0].decision, context.approvals[0].choice), (APPROVED, "calculator"))
        self.assertEqual(approved_override(context), "calculator")
        self.assertIn("explicit override: calculator", context.events[-2].detail)
        self.assertIn("built by explicit override", context.events[-1].detail)
        self.assertIn("explicitly chose", context.notes["developer_override"])
        self.assertEqual(context.notes["developer_unsupported"], "authentication, web_ui")
        self.assertEqual(self.store.load(context.run_id), context)

    def test_override_for_an_unnamed_project_type_generates_nothing(self):
        context = self.decide(self.run_to_checkpoint(AUDIT_REGRESSIONS["hotel booking"]), choice="library_api")
        self.assertEqual(context.status, FAILED)
        self.assert_nothing_written(context)

    def test_ambiguous_requirement_can_be_overridden_to_one_candidate(self):
        context = self.run_to_checkpoint("A to-do list for library staff")
        self.assertIsNone(preview(context).template)
        context = self.decide(context, choice="todo_web")
        self.assertEqual(context.status, COMPLETED)
        self.assertEqual(context.project_type, "todo_web")

    def test_rejection_ignores_any_choice(self):
        context = self.run_to_checkpoint("A calculator web page with user accounts")
        self.manager.approve(context, "A", approved=False, store=self.store, choice="calculator")
        self.assertEqual(context.approvals[0].choice, "")
        self.assertIsNone(approved_override(context))

    def test_runs_saved_before_overrides_existed_still_load(self):
        data = {"run_id": "abc123", "requirement": "x", "created_at": "t",
                "approvals": [{"checkpoint": "A", "decision": "approved", "time": "t", "note": ""}]}
        context = ProjectContext.from_dict(data)
        self.assertEqual(context.approvals[0].choice, "")
        self.assertIsNone(approved_override(context))


if __name__ == "__main__":
    unittest.main()
