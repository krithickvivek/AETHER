"""Health, configuration and the Ollama status endpoint."""

import os
import unittest
from unittest import mock

try:
    from api_support import ApiTestCase
except ImportError as error:
    if "requirements-api.txt" in str(error):
        raise
    from tests.api_support import ApiTestCase

from aether.context import SOURCE_ROOT
from aether.ollama_planner import DEFAULT_MODEL
from aether.schema import SCHEMA_VERSION

INVALID_RUNS_FOLDER = SOURCE_ROOT / "__invalid_runs_folder_for_api_tests__"  # inside AETHER's folder; never created


class HealthTests(ApiTestCase):
    def test_health_reports_the_versions_and_that_the_api_is_read_only(self):
        response = self.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "api_version": "v1", "schema_version": SCHEMA_VERSION,
                                           "read_only": True})

    def test_health_does_not_touch_the_disk(self):
        with mock.patch("aether_api.repository.RunStore", side_effect=AssertionError("opened the runs folder")), \
                mock.patch("aether_ui.history.history_path", side_effect=AssertionError("opened the history")):
            self.assertEqual(self.get("/health").status_code, 200)


class ConfigTests(ApiTestCase):
    def test_the_default_configuration(self):
        body = self.get("/config").json()
        self.assertEqual([p["key"] for p in body["planners"]], ["rule", "ollama"])
        self.assertEqual(body["default_planner"], "rule")
        self.assertEqual(body["model"], DEFAULT_MODEL)
        self.assertEqual(body["limits"], {"requirement_max_chars": 2000, "history_max_entries": 25})
        self.assertEqual([e["label"] for e in body["examples"]], ["To-do web app", "Library REST API", "CLI calculator"])
        self.assertEqual(body["warnings"], [])
        self.assertTrue(body["locations"]["runs_folder_valid"])

    def test_locations_are_short_paths_never_absolute_ones(self):
        response = self.get("/config")
        locations = response.json()["locations"]
        self.assertTrue(locations["runs"].startswith(("~/", "…/")), locations)
        self.assertTrue(locations["history"].startswith(("~/", "…/")), locations)
        self.assert_no_private_text(response)

    def test_the_planner_default_follows_aether_planner(self):
        with mock.patch.dict(os.environ, {"AETHER_PLANNER": "ollama"}):
            body = self.get("/config").json()
        self.assertEqual(body["default_planner"], "ollama")
        self.assertEqual(body["warnings"], [])

    def test_an_invalid_planner_setting_falls_back_to_rule_with_a_warning(self):
        with mock.patch.dict(os.environ, {"AETHER_PLANNER": "magic"}):
            body = self.get("/config").json()
        self.assertEqual(body["default_planner"], "rule")
        self.assertEqual([w["code"] for w in body["warnings"]], ["invalid_planner_setting"])
        self.assertIn("must be one of rule, ollama", body["warnings"][0]["message"])

    def test_an_invalid_runs_folder_is_reported_as_a_persistent_warning(self):
        self.assertFalse(INVALID_RUNS_FOLDER.exists())
        with mock.patch.dict(os.environ, {"AETHER_RUNS_DIR": str(INVALID_RUNS_FOLDER)}):
            first, second = self.get("/config"), self.get("/config")  # the same on every request (not a one-off)
        for response in (first, second):
            body = response.json()
            self.assertEqual([w["code"] for w in body["warnings"]], ["runs_folder_invalid"])
            self.assertFalse(body["locations"]["runs_folder_valid"])
            self.assertIsNone(body["locations"]["runs"])
            self.assert_no_private_text(response)
        self.assertEqual(first.json(), second.json())
        self.assertFalse(INVALID_RUNS_FOLDER.exists(), "reading the configuration must not create the folder")


class OllamaStatusTests(ApiTestCase):
    def test_ready_when_ollama_has_the_model(self):
        with mock.patch("aether_api.routers.config.ollama_status",
                        return_value=(True, f"Ollama is running with {DEFAULT_MODEL}.")) as status:
            body = self.get("/planners/ollama/status").json()
        self.assertEqual(body, {"ready": True, "message": f"Ollama is running with {DEFAULT_MODEL}.",
                                "model": DEFAULT_MODEL})
        status.assert_called_once()

    def test_not_ready_is_a_normal_answer_not_an_error(self):
        with mock.patch("aether_api.routers.config.ollama_status",
                        return_value=(False, "Ollama is not running on this computer.")):
            response = self.get("/planners/ollama/status")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ready"])

    def test_the_answer_is_cached_for_a_few_seconds(self):
        with mock.patch("aether_api.routers.config.ollama_status", return_value=(False, "down")) as status:
            for _ in range(3):
                self.get("/planners/ollama/status")
        self.assertEqual(status.call_count, 1)

    def test_the_cache_expires(self):
        clock = [100.0]
        self.app.state.ollama_cache.clock = lambda: clock[0]
        with mock.patch("aether_api.routers.config.ollama_status", return_value=(False, "down")) as status:
            self.get("/planners/ollama/status")
            clock[0] += 14
            self.get("/planners/ollama/status")
            clock[0] += 2
            self.get("/planners/ollama/status")
        self.assertEqual(status.call_count, 2)

    def test_with_the_network_blocked_the_real_check_says_ollama_is_not_running(self):
        body = self.get("/planners/ollama/status").json()  # urlopen is blocked by the test set-up
        self.assertEqual(body, {"ready": False, "message": "Ollama is not running on this computer.",
                                "model": DEFAULT_MODEL})


if __name__ == "__main__":
    unittest.main()
