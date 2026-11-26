"""GET /runs/{run_id} and GET /runs/{run_id}/checkpoint: run state, failures, fallback, previews, unreadable data."""

import json
import os
import unittest
from pathlib import Path
from unittest import mock

try:
    from api_support import AMBIGUOUS, CALCULATOR, TODO, TODO_WITH_API, ApiTestCase
except ImportError as error:
    if "requirements-api.txt" in str(error):
        raise
    from tests.api_support import AMBIGUOUS, CALCULATOR, TODO, TODO_WITH_API, ApiTestCase

from aether import workspace
from aether.agents import PlannerAgent
from aether.context import SOURCE_ROOT, RunStore
from aether.developer import DeveloperAgent
from aether.fallback import FallbackPlanner
from aether.manager import ManagerAgent
from aether.planner import Planner, RuleBasedPlanner

INVALID_RUNS_FOLDER = SOURCE_ROOT / "__invalid_runs_folder_for_api_tests__"


class Boom(Planner):
    name = "Boom"

    def create_plan(self, brief):
        raise RuntimeError("boom")


class FailingModel:
    name = "OllamaPlanner"

    def create_plan(self, brief):
        raise RuntimeError("model unavailable")


def statuses(body) -> list:
    return [(step["title"], step["status"]) for step in body["progress"]]


class RunViewTests(ApiTestCase):
    def test_a_run_waiting_at_checkpoint_a(self):
        context, _ = self.make_run(TODO, save_history=False)
        response = self.get(f"/runs/{context.run_id}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["source"], body["run_id"], body["entry_id"], body["context_state"]),
                         ("run", context.run_id, None, "ok"))
        self.assertEqual(body["status"], "awaiting_approval")
        self.assertEqual(body["requirement"], TODO)
        self.assertEqual(body["started_at"], context.created_at)  # the core's naive local timestamp, unchanged
        self.assertTrue(body["ok"])
        self.assertEqual(statuses(body), [("Requirement", "done"), ("Plan", "done"), ("Approval", "current"),
                                          ("Generated files", "pending")])
        self.assertEqual(body["progress"][2]["detail"], "Waiting for you")
        self.assertEqual(body["files"], [])
        self.assertEqual(body["approvals"], [])
        self.assertIsNone(body["developer_error"])
        self.assertEqual(body["workspace"]["exists"], False)
        self.assertTrue(body["workspace"]["display_path"].endswith(f"{context.run_id}/workspace"))
        self.assertEqual(body["actions"], {"can_approve": True, "can_reject": True, "can_override": False,
                                           "disabled_reason": None})
        self.assertIsNone(body["duration_ms"], "no history snapshot, so the run time is unknown")
        self.assertIsNone(body["manager_log"])
        self.assertIsNone(body["stages"])
        self.assert_no_private_text(response)

    def test_the_plan_is_numbered_grouped_and_tagged_with_its_origin(self):
        context, _ = self.make_run(TODO, save_history=False)
        plan = self.get(f"/runs/{context.run_id}").json()["plan"]
        self.assertEqual((plan["planner_name"], plan["ai_generated"]), ("RuleBasedPlanner", False))
        self.assertEqual([s["number"] for s in plan["steps"]], list(range(1, len(plan["steps"]) + 1)))
        self.assertEqual([p["name"] for p in plan["phases"]],
                         ["Requirements", "Design", "Setup", "Implementation", "Testing", "Documentation"])
        self.assertEqual(sorted(n for p in plan["phases"] for n in p["step_numbers"]),
                         [s["number"] for s in plan["steps"]])
        origins = {s["origin"] for s in plan["steps"]}
        self.assertIn("core", origins)
        self.assertTrue({"authentication", "database", "web_ui"} <= origins)

    def test_the_brief_lists_the_detected_features_with_labels(self):
        context, _ = self.make_run(TODO, save_history=False)
        brief = self.get(f"/runs/{context.run_id}").json()["brief"]
        self.assertEqual(brief["requirement"], TODO)
        self.assertEqual([(f["key"], f["label"]) for f in brief["features"]],
                         [("authentication", "Authentication"), ("database", "Database / storage"),
                          ("web_ui", "Web interface")])

    def test_run_time_and_the_manager_log_come_only_from_a_history_snapshot(self):
        context, entry = self.make_run(CALCULATOR, save_history=True)
        body = self.get(f"/runs/{context.run_id}").json()
        self.assertIsInstance(body["duration_ms"], float)
        self.assertGreaterEqual(body["duration_ms"], 0)
        self.assertTrue(any("Requirement received" in line for line in body["manager_log"]))

    def test_a_completed_run_with_its_files_and_activity(self):
        context, _ = self.make_run(CALCULATOR, approve=True)
        response = self.get(f"/runs/{context.run_id}")
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(statuses(body), [("Requirement", "done"), ("Plan", "done"), ("Approval", "done"),
                                          ("Generated files", "done")])
        self.assertEqual(sorted(f["path"] for f in body["files"]),
                         ["README.md", "calculator.py", "tests/__init__.py", "tests/test_calculator.py"])
        self.assertEqual({f["language"] for f in body["files"]}, {"python", "markdown"})
        self.assertEqual(body["project_type"], "calculator")
        self.assertEqual(body["template_title"], "Command-line calculator")
        self.assertEqual([(a["checkpoint"], a["decision"]) for a in body["approvals"]], [("A", "approved")])
        self.assertIn(("Developer", "done"), [(e["agent"], e["status"]) for e in body["events"]])
        self.assertTrue(body["workspace"]["exists"])
        self.assertEqual(body["actions"]["disabled_reason"], "This run is not waiting for approval.")
        self.assertFalse(body["actions"]["can_approve"])
        self.assertIsNone(body["developer_error"])
        self.assert_no_private_text(response)

    def test_a_rejected_run_is_cancelled_state_not_an_error(self):
        context, _ = self.make_run(CALCULATOR)
        self.reject(context)
        body = self.get(f"/runs/{context.run_id}").json()
        self.assertEqual(body["status"], "cancelled")
        self.assertEqual(statuses(body)[2:], [("Approval", "failed"), ("Generated files", "pending")])
        self.assertEqual(body["approvals"][0]["decision"], "rejected")
        self.assertEqual(body["files"], [])

    def test_an_explicit_override_is_recorded(self):
        context, _ = self.make_run(AMBIGUOUS, approve=True, override="todo_web")
        body = self.get(f"/runs/{context.run_id}").json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["approvals"][0]["choice"], "todo_web")
        self.assertIn("explicitly chose", body["notes"]["developer_override"])

    def test_the_workspace_path_is_a_short_display_form(self):
        context, _ = self.make_run(CALCULATOR, approve=True)
        shown = self.get(f"/runs/{context.run_id}").json()["workspace"]["display_path"]
        self.assertTrue(shown.startswith(("~/", "…/")), shown)
        self.assertNotIn(":", shown)


class FailedRunTests(ApiTestCase):
    """Domain failures are run STATE (status, progress, error fields), never HTTP errors."""

    def test_a_requirement_the_manager_refuses(self):
        context, entry = self.make_run("   ")
        self.assertIsNone(entry, "a failed plan is not saved to history")
        response = self.get(f"/runs/{context.run_id}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "failed")
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "The requirement is empty. Please describe the software you want.")
        self.assertIsNone(body["error_details"])
        self.assertIsNone(body["plan"])
        self.assertIsNone(body["brief"])
        self.assertEqual(statuses(body)[0], ("Requirement", "failed"))
        self.assertEqual(statuses(body)[1], ("Plan", "pending"))
        self.assertFalse(body["actions"]["can_approve"])

    def test_a_planner_that_crashes(self):
        context, _ = self.make_run(CALCULATOR, planner=Boom(), save_history=False)
        body = self.get(f"/runs/{context.run_id}").json()
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error"], "Something went wrong while generating the plan.")
        self.assertEqual(body["error_details"], "RuntimeError: boom")
        self.assertEqual(statuses(body)[:2], [("Requirement", "done"), ("Plan", "failed")])
        self.assertIsNotNone(body["brief"])
        self.assertIsNone(body["plan"])

    def test_the_developer_cannot_write_the_first_file(self):
        context, _ = self.make_run(CALCULATOR)
        with mock.patch.object(workspace, "MAX_FILE_BYTES", 10):
            from aether_ui.workflow import decide_plan
            decide_plan(context, approved=True, store=RunStore())
        body = self.get(f"/runs/{context.run_id}").json()
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["files"], [])
        self.assertTrue(body["developer_error"].startswith("Could not write project files:"), body["developer_error"])
        self.assertEqual(statuses(body)[3], ("Generated files", "failed"))
        self.assertTrue(body["ok"], "the plan itself was fine")

    def test_a_partial_write_failure_now_says_so(self):
        """The Streamlit page shows the partial files with no message (K38); the API always reports the failure."""
        context, _ = self.make_run(CALCULATOR)
        with mock.patch.object(workspace, "MAX_FILES", 1):
            from aether_ui.workflow import decide_plan
            decide_plan(context, approved=True, store=RunStore())
        body = self.get(f"/runs/{context.run_id}").json()
        self.assertEqual(body["status"], "failed")
        self.assertEqual([f["path"] for f in body["files"]], ["calculator.py"])
        self.assertIn("maximum of 1 files", body["developer_error"])


class PlannerFallbackTests(ApiTestCase):
    def test_the_fallback_reason_comes_from_the_run_notes(self):
        store = RunStore()
        manager = ManagerAgent(RuleBasedPlanner(), log=lambda message: None)
        agents = [PlannerAgent(FallbackPlanner(FailingModel(), RuleBasedPlanner())), DeveloperAgent(store)]
        context = manager.run_pipeline(CALCULATOR, agents, store=store)
        self.assertEqual(context.notes["planner_fallback"], "model unavailable")

        body = self.get(f"/runs/{context.run_id}").json()
        self.assertEqual(body["fallback_reason"], "model unavailable")
        self.assertEqual(body["notes"]["planner_fallback"], "model unavailable")
        self.assertEqual(body["plan"]["planner_name"], "RuleBasedPlanner")
        self.assertFalse(body["ai_generated"])
        self.assertIn(("Planner", "failed"), [(e["agent"], e["status"]) for e in body["events"]])  # the failed attempt

    def test_the_fallback_reason_comes_from_the_history_snapshot_when_the_run_has_no_note(self):
        """start_project (the path both UIs use) wraps the planner in a TracingPlanner, which hides
        `fallback_reason` from PlannerAgent, so such runs carry no note; the history snapshot still has it."""
        context, _ = self.make_run(CALCULATOR, planner=FallbackPlanner(FailingModel(), RuleBasedPlanner()))
        self.assertNotIn("planner_fallback", context.notes)  # PINNED core behaviour (reported, not changed here)
        body = self.get(f"/runs/{context.run_id}").json()
        self.assertEqual(body["fallback_reason"], "model unavailable")

    def test_no_fallback_for_a_normal_plan(self):
        context, _ = self.make_run(CALCULATOR)
        self.assertIsNone(self.get(f"/runs/{context.run_id}").json()["fallback_reason"])


class UnreadableRunTests(ApiTestCase):
    def test_an_unknown_run_is_a_404_before_anything_else(self):
        self.assert_error(self.get("/runs/abcdef123456"), 404, "run_not_found")

    def test_ids_that_are_not_letters_and_digits_are_a_404(self):
        for run_id in ("bad.id", "a%20b", "a-b", "a_b", "caf%C3%A9!"):
            with self.subTest(run_id=run_id):
                self.assert_error(self.get(f"/runs/{run_id}"), 404, "run_not_found")

    def test_a_very_long_id_is_an_invalid_request(self):
        error = self.assert_error(self.get("/runs/" + "a" * 65), 422, "invalid_request")
        self.assertEqual(error["details"][0]["location"], "path.run_id")

    def test_damaged_json_is_a_422_that_says_which_run(self):
        context, _ = self.make_run(CALCULATOR)
        (self.run_dir(context.run_id) / "context.json").write_text("{broken", encoding="utf-8")
        error = self.assert_error(self.get(f"/runs/{context.run_id}"), 422, "run_unreadable")
        self.assertIn(context.run_id, error["message"])
        self.assertIn("not valid JSON", error["message"])

    def test_a_newer_schema_is_a_422_and_the_file_is_not_changed(self):
        context, _ = self.make_run(CALCULATOR)
        self.write_context(context.run_id, schema_version=99)
        path = self.run_dir(context.run_id) / "context.json"
        before = path.read_bytes()
        error = self.assert_error(self.get(f"/runs/{context.run_id}"), 422, "run_unreadable")
        self.assertIn("newer AETHER", error["message"])
        self.assertEqual(path.read_bytes(), before)

    def test_a_record_missing_essential_fields_is_a_422(self):
        context, _ = self.make_run(CALCULATOR)
        (self.run_dir(context.run_id) / "context.json").write_text('{"schema_version": 1}', encoding="utf-8")
        error = self.assert_error(self.get(f"/runs/{context.run_id}"), 422, "run_unreadable")
        self.assertIn("missing required field", error["message"])

    def test_invalid_utf8_is_a_422(self):
        context, _ = self.make_run(CALCULATOR)
        (self.run_dir(context.run_id) / "context.json").write_bytes(b"\xff\xfe\x00{}")
        self.assert_error(self.get(f"/runs/{context.run_id}"), 422, "run_unreadable")

    def test_unknown_fields_are_tolerated_and_reported_as_warnings_without_changing_the_file(self):
        context, _ = self.make_run(CALCULATOR)
        self.write_context(context.run_id, future_field={"x": 1})
        path = self.run_dir(context.run_id) / "context.json"
        before = path.read_bytes()
        body = self.get(f"/runs/{context.run_id}").json()
        self.assertTrue(any("unknown field 'future_field'" in w for w in body["warnings"]), body["warnings"])
        self.assertEqual(path.read_bytes(), before, "unknown fields must stay exactly as saved")

    def test_a_lock_that_clears_is_retried_and_one_that_stays_is_a_503(self):
        context, _ = self.make_run(CALCULATOR, save_history=False)
        real = Path.read_text
        context_file = self.run_dir(context.run_id) / "context.json"
        calls = []

        def locked_once(path, *args, **kwargs):
            if Path(path) == context_file and not calls:
                calls.append(1)
                raise PermissionError("locked")
            return real(path, *args, **kwargs)

        def always_locked(path, *args, **kwargs):
            if Path(path) == context_file:
                raise PermissionError("locked")
            return real(path, *args, **kwargs)

        with mock.patch("aether_api.repository.time.sleep"), mock.patch.object(Path, "read_text", locked_once):
            self.assertEqual(self.get(f"/runs/{context.run_id}").status_code, 200)
        with mock.patch("aether_api.repository.time.sleep"), mock.patch.object(Path, "read_text", always_locked):
            self.assert_error(self.get(f"/runs/{context.run_id}"), 503, "storage_unavailable")

    def test_with_an_invalid_runs_folder_every_run_endpoint_is_a_503_without_a_path(self):
        context, _ = self.make_run(CALCULATOR)
        self.assertFalse(INVALID_RUNS_FOLDER.exists())
        with mock.patch.dict(os.environ, {"AETHER_RUNS_DIR": str(INVALID_RUNS_FOLDER)}):
            for path in (f"/runs/{context.run_id}", f"/runs/{context.run_id}/checkpoint",
                         f"/runs/{context.run_id}/files", f"/runs/{context.run_id}/plan.md",
                         f"/runs/{context.run_id}/files/content?path=calculator.py"):
                with self.subTest(path=path):
                    error = self.assert_error(self.get(path), 503, "runs_folder_invalid")
                    self.assertIn("AETHER_RUNS_DIR", error["message"])
        self.assertFalse(INVALID_RUNS_FOLDER.exists())

    def test_text_from_saved_data_is_scrubbed_of_the_home_folder(self):
        context, _ = self.make_run(CALCULATOR, save_history=False)
        path = self.run_dir(context.run_id) / "context.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["events"][0]["detail"] = f"opened {Path.home()}\\secret\\file.txt"
        data["notes"]["x"] = f"in {str(Path.home()).replace(chr(92), '/')}/x"
        path.write_text(json.dumps(data), encoding="utf-8")
        response = self.get(f"/runs/{context.run_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("opened ~\\secret\\file.txt", response.json()["events"][0]["detail"])
        self.assertTrue(response.json()["notes"]["x"].startswith("in ~/"))
        self.assert_no_private_text(response)


class CheckpointPreviewTests(ApiTestCase):
    def test_a_requirement_that_fits(self):
        context, _ = self.make_run(CALCULATOR)
        response = self.get(f"/runs/{context.run_id}/checkpoint")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["available"], body["awaiting_approval"], body["status"]),
                         (True, True, "awaiting_approval"))
        self.assertEqual(body["template"]["key"], "calculator")
        self.assertEqual(body["fit"]["status"], "selected")
        self.assertEqual(body["fit"]["badge"], {"label": "Fits the requirement", "tone": "ok"})
        self.assertEqual([f["path"] for f in body["planned_files"]],
                         ["calculator.py", "tests/__init__.py", "tests/test_calculator.py", "README.md"])
        self.assertEqual(body["uncovered_steps"], [])
        self.assertEqual(body["candidates"], [])
        self.assertIsNone(body["override"])
        self.assertTrue(body["actions"]["can_approve"])
        self.assertTrue(any(f["covers"] for f in body["planned_files"]))

    def test_a_partial_fit_names_what_is_not_covered(self):
        context, _ = self.make_run(TODO_WITH_API)
        body = self.get(f"/runs/{context.run_id}/checkpoint").json()
        self.assertEqual(body["fit"]["badge"], {"label": "Partial fit", "tone": "warn"})
        self.assertEqual([f["key"] for f in body["fit"]["unsupported"]], ["api"])
        self.assertEqual(body["fit"]["unsupported"][0]["label"], "API")
        self.assertTrue(body["uncovered_steps"])
        self.assertTrue(body["actions"]["can_approve"], "a partial fit can still be approved")

    def test_an_ambiguous_requirement_has_no_template_and_offers_an_override(self):
        context, _ = self.make_run(AMBIGUOUS)
        body = self.get(f"/runs/{context.run_id}/checkpoint").json()
        self.assertIsNone(body["template"])
        self.assertEqual(body["fit"]["status"], "ambiguous")
        self.assertEqual(body["fit"]["badge"]["label"], "Ambiguous: more than one project type")
        self.assertEqual(body["planned_files"], [])
        self.assertEqual(sorted(c["key"] for c in body["candidates"]), ["library_api", "todo_web"])
        self.assertEqual(body["actions"]["can_approve"], False)
        self.assertEqual(body["actions"]["can_reject"], True)
        self.assertEqual(body["actions"]["can_override"], True)
        self.assertIn("names more than one project type", body["actions"]["disabled_reason"])

    def test_the_consequences_of_an_override_can_be_previewed(self):
        context, _ = self.make_run(AMBIGUOUS)
        body = self.get(f"/runs/{context.run_id}/checkpoint?override=todo_web").json()
        self.assertEqual(body["override"]["key"], "todo_web")
        lines = body["override"]["consequences"]
        self.assertTrue(lines[0].startswith("The project will be a "), lines)
        self.assertIn("recorded as an explicit override", lines[-1])
        self.assertIsNone(body["template"], "previewing an override must not choose it")

    def test_an_override_that_is_not_allowed_is_a_422(self):
        fitting, _ = self.make_run(CALCULATOR)
        self.assert_error(self.get(f"/runs/{fitting.run_id}/checkpoint?override=todo_web"), 422, "invalid_override")
        ambiguous, _ = self.make_run(AMBIGUOUS)
        self.assert_error(self.get(f"/runs/{ambiguous.run_id}/checkpoint?override=calculator"), 422, "invalid_override")
        self.assert_error(self.get(f"/runs/{ambiguous.run_id}/checkpoint?override=nonsense"), 422, "invalid_override")

    def test_the_preview_is_read_only(self):
        context, _ = self.make_run(AMBIGUOUS)
        path = self.run_dir(context.run_id) / "context.json"
        before = {p: p.read_bytes() for p in self.run_dir(context.run_id).rglob("*") if p.is_file()}
        for query in ("", "?override=todo_web"):
            self.get(f"/runs/{context.run_id}/checkpoint{query}")
        self.assertEqual({p: p.read_bytes() for p in self.run_dir(context.run_id).rglob("*") if p.is_file()}, before)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "awaiting_approval")
        self.assertEqual(self.get(f"/runs/{context.run_id}").json()["approvals"], [])
        self.assertFalse((self.run_dir(context.run_id) / "workspace").exists())

    def test_a_completed_run_shows_what_was_built_not_an_approval_prompt(self):
        context, _ = self.make_run(CALCULATOR, approve=True)
        body = self.get(f"/runs/{context.run_id}/checkpoint").json()
        self.assertEqual((body["available"], body["awaiting_approval"], body["status"]), (True, False, "completed"))
        self.assertEqual(body["template"]["key"], "calculator")
        self.assertFalse(body["actions"]["can_approve"])

    def test_a_run_built_by_override_still_previews_its_template(self):
        context, _ = self.make_run(AMBIGUOUS, approve=True, override="todo_web")
        body = self.get(f"/runs/{context.run_id}/checkpoint").json()
        self.assertEqual((body["template"]["key"], body["fit"]["status"]), ("todo_web", "overridden"))

    def test_a_run_without_a_plan_has_no_preview(self):
        context, _ = self.make_run("   ")
        body = self.get(f"/runs/{context.run_id}/checkpoint").json()
        self.assertEqual((body["available"], body["template"], body["fit"], body["planned_files"]), (False, None, None, []))
        self.assertTrue(body["reason"])

    def test_a_missing_run_is_a_404(self):
        self.assert_error(self.get("/runs/abcdef123456/checkpoint"), 404, "run_not_found")

    def test_start_project_is_not_called_by_any_read(self):
        context, _ = self.make_run(CALCULATOR)
        with mock.patch("aether_ui.workflow.start_project", side_effect=AssertionError("planned")), \
                mock.patch("aether_ui.workflow.decide_plan", side_effect=AssertionError("decided")), \
                mock.patch("aether.manager.ManagerAgent.approve", side_effect=AssertionError("approved")), \
                mock.patch("aether.manager.ManagerAgent.resume", side_effect=AssertionError("resumed")):
            self.assertEqual(self.get(f"/runs/{context.run_id}/checkpoint").status_code, 200)
            self.assertEqual(self.get(f"/runs/{context.run_id}").status_code, 200)


if __name__ == "__main__":
    unittest.main()
