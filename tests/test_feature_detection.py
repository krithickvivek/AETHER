"""Tests for aether/feature_detection.py (Task 004): behaviour pinned to what it was in the Manager."""

import ast
import subprocess
import sys
import unittest
from pathlib import Path

from aether import feature_detection, manager, ollama_planner, templates
from aether.feature_detection import FEATURES, FEATURE_KEYWORDS, detect_features, mentions_feature
from aether.models import PlanStep

ROOT = Path(__file__).resolve().parent.parent


def step(text: str, phase: str = "Implementation") -> PlanStep:
    return PlanStep(phase, text)


class PinnedVocabularyTests(unittest.TestCase):
    """These tables are the behaviour. Changing them changes plans and template choices, so a change must be deliberate."""

    def test_requirement_keywords_are_unchanged(self):
        self.assertEqual(FEATURE_KEYWORDS, {
            "authentication": ["login", "log in", "logs in", "sign in", "sign up", "signup",
                               "register", "password", "account", "accounts", "authentication"],
            "database": ["database", "db", "save", "saves", "saved", "store", "stores", "stored",
                         "persist", "record", "records"],
            "web_ui": ["website", "web", "webpage", "web page", "frontend", "browser", "dashboard"],
            "api": ["api", "apis", "endpoint", "endpoints"],
        })

    def test_plan_step_keywords_are_unchanged(self):
        self.assertEqual(feature_detection.EXTRA_KEYWORDS, {
            "authentication": ["auth", "authenticate", "authentication", "sign-in", "signin", "credentials",
                               "logout", "registration", "session", "sessions"],
            "database": ["data model", "schema", "crud", "storage", "persistence", "saving", "storing", "loading"],
            "web_ui": ["ui", "user interface", "interface", "page", "pages", "form", "forms", "front-end",
                       "frontend"],
            "api": ["rest", "http", "request", "requests", "response", "responses"],
        })

    def test_the_four_features_in_their_reporting_order(self):
        self.assertEqual(FEATURES, ("authentication", "database", "web_ui", "api"))


class DetectFeaturesTests(unittest.TestCase):
    def test_every_requirement_keyword_detects_its_feature(self):
        for feature, keywords in FEATURE_KEYWORDS.items():
            for keyword in keywords:
                with self.subTest(feature=feature, keyword=keyword):
                    self.assertIn(feature, detect_features(f"I want {keyword} please"))

    def test_all_four_features_come_back_in_fixed_order_whatever_the_text_order(self):
        self.assertEqual(detect_features("an api with a website, a database and login"),
                         ["authentication", "database", "web_ui", "api"])

    def test_no_features(self):
        for text in ("", "   ", "A command-line calculator", "Show the capital of each country"):
            with self.subTest(text=text):
                self.assertEqual(detect_features(text), [])

    def test_matching_ignores_case(self):
        self.assertEqual(detect_features("LOGIN With An API"), ["authentication", "api"])

    def test_matches_whole_words_only(self):
        for text, expected in {
            "A webinar about capital cities": [],       # "web" inside "webinar", "api" inside "capital"
            "a therapist": [],                           # "api" inside "therapist"
            "a dbms course": [],                         # "db" inside "dbms"
            "the recorded lecture": [],                  # "record" is not "recorded"
            "an accountant": [],                         # "account" inside "accountant"
            "A web-based tool": ["web_ui"],              # a hyphen is a word boundary
            "uses a DB": ["database"],
            "an API/endpoint listing": ["api"],
        }.items():
            with self.subTest(text=text):
                self.assertEqual(detect_features(text), expected)

    def test_multi_word_keywords(self):
        self.assertEqual(detect_features("users log in and sign up"), ["authentication"])
        self.assertEqual(detect_features("a web page"), ["web_ui"])
        self.assertEqual(detect_features("users log  in"), [])  # the keyword is matched as written; the Manager
        # collapses whitespace in the requirement before detecting.

    def test_each_feature_appears_once(self):
        self.assertEqual(detect_features("login login login password account"), ["authentication"])

    def test_punctuation_and_non_ascii_text(self):
        self.assertEqual(detect_features("Login, (database); API! — café"), ["authentication", "database", "api"])
        self.assertEqual(detect_features("ログイン 日本語"), [])

    def test_examples_used_by_the_workspace(self):
        self.assertEqual(detect_features("Build a to-do website where users can log in and save their tasks"),
                         ["authentication", "database", "web_ui"])
        self.assertEqual(detect_features("Create a REST API for a library that stores books and members in a database"),
                         ["database", "api"])
        self.assertEqual(detect_features("A command-line calculator for basic arithmetic"), [])

    def test_does_not_use_plan_step_words(self):
        """"schema" or "session" describe a plan step, but do not mean the user asked for the feature."""
        self.assertEqual(detect_features("a schema and a session"), [])


class MentionsFeatureTests(unittest.TestCase):
    def test_uses_requirement_and_plan_step_words(self):
        self.assertTrue(mentions_feature("authentication", step("Implement registration and logout")))
        self.assertTrue(mentions_feature("authentication", step("Hash each password")))
        self.assertTrue(mentions_feature("database", step("Design the data model")))
        self.assertTrue(mentions_feature("database", step("Write the schema")))
        self.assertTrue(mentions_feature("web_ui", step("Build the web pages")))
        self.assertTrue(mentions_feature("api", step("Validate each HTTP request")))

    def test_whole_words_only_and_case_insensitive(self):
        self.assertFalse(mentions_feature("api", step("Plan the capital budget")))
        self.assertFalse(mentions_feature("web_ui", step("Write a user guide")))
        self.assertTrue(mentions_feature("web_ui", step("BUILD THE PAGES")))

    def test_looks_at_the_description_only_not_the_phase(self):
        self.assertFalse(mentions_feature("web_ui", PlanStep("Web pages", "Write the tests")))

    def test_unknown_feature_is_never_mentioned(self):
        self.assertFalse(mentions_feature("machine_learning", step("Train a model with login")))

    def test_a_feature_is_not_confused_with_another(self):
        self.assertFalse(mentions_feature("api", step("Build the web pages")))
        self.assertFalse(mentions_feature("authentication", step("Set up the database")))


class CompatibilityAndCouplingTests(unittest.TestCase):
    def test_old_import_locations_still_work_and_are_the_same_objects(self):
        self.assertIs(manager.detect_features, detect_features)
        self.assertIs(manager.FEATURE_KEYWORDS, FEATURE_KEYWORDS)
        self.assertIs(ollama_planner.mentions_feature, mentions_feature)
        self.assertIs(ollama_planner.FEATURE_KEYWORDS, FEATURE_KEYWORDS)
        self.assertIs(ollama_planner.EXTRA_KEYWORDS, feature_detection.EXTRA_KEYWORDS)
        self.assertIs(templates.mentions_feature, mentions_feature)
        self.assertEqual(templates.FEATURES, FEATURES)

    def test_manager_uses_the_module_for_the_task_brief(self):
        brief = manager.ManagerAgent(planner=None).understand("A website  with   login")
        self.assertEqual(brief.requirement, "A website with login")
        self.assertEqual(brief.features, ["authentication", "web_ui"])

    def test_the_module_depends_only_on_the_standard_library_and_models(self):
        tree = ast.parse((ROOT / "aether" / "feature_detection.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
        self.assertEqual(imported, {"re", "aether.models"})

    def test_planners_and_templates_no_longer_import_the_manager_or_each_other_for_detection(self):
        def imports(path: str) -> set:
            tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
            return {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}

        self.assertNotIn("aether.manager", imports("aether/ollama_planner.py"))
        self.assertNotIn("aether.ollama_planner", imports("aether/templates/__init__.py"))

    def test_every_module_imports_cleanly_on_its_own(self):
        """Run each import in a fresh interpreter, so an import cycle cannot hide behind modules loaded earlier."""
        for name in ("aether.feature_detection", "aether.manager", "aether.ollama_planner", "aether.templates",
                     "aether.planner", "aether.agents", "aether.developer", "aether.config", "aether_ui.workflow"):
            with self.subTest(module=name):
                done = subprocess.run([sys.executable, "-c", f"import {name}"], cwd=ROOT,
                                      capture_output=True, text=True, timeout=60)
                self.assertEqual(done.returncode, 0, done.stderr)

    def test_importing_the_module_loads_no_orchestration_planning_or_template_code(self):
        code = ("import sys, aether.feature_detection; "
                "bad = [m for m in ('aether.manager', 'aether.ollama_planner', 'aether.templates', 'aether.planner', "
                "'aether.agents', 'aether.developer') if m in sys.modules]; "
                "sys.exit(','.join(bad) or 0)")
        done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, f"feature_detection pulled in: {done.stderr or done.stdout}")


if __name__ == "__main__":
    unittest.main()
