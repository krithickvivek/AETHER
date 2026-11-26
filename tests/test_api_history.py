"""GET /history and GET /history/{entry_id}: entries, snapshots, and honest reporting of damaged data (C6)."""

import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

try:
    from api_support import ApiTestCase, CALCULATOR, TODO, TODO_WITH_API
except ImportError as error:
    if "requirements-api.txt" in str(error):
        raise
    from tests.api_support import ApiTestCase, CALCULATOR, TODO, TODO_WITH_API

from aether.context import SOURCE_ROOT
from aether_ui import history
from aether_ui.workflow import start_project

INVALID_RUNS_FOLDER = SOURCE_ROOT / "__invalid_runs_folder_for_api_tests__"


class HistoryListTests(ApiTestCase):
    def test_no_history_file_is_a_healthy_empty_history(self):
        body = self.get("/history").json()
        self.assertEqual(body, {"file_state": "missing", "entries": [], "problems": [], "max_entries": 25})

    def test_a_saved_plan_is_listed_with_its_stored_run(self):
        context, entry = self.make_run(TODO)
        body = self.get("/history").json()
        self.assertEqual(body["file_state"], "ok")
        self.assertEqual(body["problems"], [])
        item = body["entries"][0]
        self.assertEqual(item["entry_id"], entry["id"])
        self.assertEqual(item["run_id"], context.run_id)
        self.assertEqual(item["requirement"], TODO)
        self.assertEqual(item["planner_name"], "RuleBasedPlanner")
        self.assertFalse(item["ai_generated"])
        self.assertIsNone(item["fallback_reason"])
        self.assertGreater(item["step_count"], 5)
        self.assertEqual([f["key"] for f in item["features"]], ["authentication", "database", "web_ui"])
        self.assertEqual(item["features"][0]["label"], "Authentication")
        self.assertEqual(item["context_state"], "ok")
        self.assertEqual(item["run_status"], "awaiting_approval")
        self.assertEqual(item["file_count"], 0)

    def test_the_status_and_file_count_follow_the_stored_run(self):
        self.make_run(CALCULATOR, approve=True)
        item = self.get("/history").json()["entries"][0]
        self.assertEqual((item["run_status"], item["file_count"]), ("completed", 4))

    def test_entries_are_newest_first(self):
        self.make_run(CALCULATOR)
        self.make_run(TODO)
        self.assertEqual([e["requirement"] for e in self.get("/history").json()["entries"]], [TODO, CALCULATOR])

    def test_a_run_folder_that_is_gone_is_reported_as_missing(self):
        context, _ = self.make_run(CALCULATOR)
        shutil.rmtree(self.run_dir(context.run_id))
        item = self.get("/history").json()["entries"][0]
        self.assertEqual((item["context_state"], item["run_status"], item["file_count"]), ("missing", None, None))

    def test_a_damaged_run_record_is_reported_as_unreadable(self):
        context, _ = self.make_run(CALCULATOR)
        (self.run_dir(context.run_id) / "context.json").write_text("{not json", encoding="utf-8")
        item = self.get("/history").json()["entries"][0]
        self.assertEqual((item["context_state"], item["run_status"]), ("unreadable", None))

    def test_a_newer_run_record_is_reported_as_unreadable(self):
        context, _ = self.make_run(CALCULATOR)
        self.write_context(context.run_id, schema_version=99)
        self.assertEqual(self.get("/history").json()["entries"][0]["context_state"], "unreadable")

    def test_a_plan_saved_without_a_stored_run_has_no_run_id(self):
        result, _ = start_project(CALCULATOR, store=None)  # as when the runs folder was unusable
        history.save_run(result)
        item = self.get("/history").json()["entries"][0]
        self.assertEqual((item["run_id"], item["context_state"], item["run_status"]), (None, "missing", None))

    def test_the_fallback_reason_is_listed(self):
        class Failing:
            name = "OllamaPlanner"

            def create_plan(self, brief):
                raise RuntimeError("model unavailable")

        from aether.fallback import FallbackPlanner
        from aether.planner import RuleBasedPlanner
        self.make_run(CALCULATOR, planner=FallbackPlanner(Failing(), RuleBasedPlanner()))
        item = self.get("/history").json()["entries"][0]
        self.assertEqual(item["fallback_reason"], "model unavailable")
        self.assertEqual(item["planner_name"], "RuleBasedPlanner")


class DamagedHistoryTests(ApiTestCase):
    """A damaged history must never look like an empty, healthy one, and reading it must never rewrite it."""

    def assert_history_untouched(self, expected: bytes):
        self.assertEqual(self.history_file.read_bytes(), expected)
        self.assertEqual([p.name for p in self.base.iterdir() if "damaged" in p.name], [],
                         "a read must not copy the file aside as save_run does")

    def test_invalid_json_is_reported_as_damaged_not_empty(self):
        self.history_file.write_bytes(b"{this is not json")
        body = self.get("/history").json()
        self.assertEqual((body["file_state"], body["entries"]), ("damaged", []))
        self.assert_history_untouched(b"{this is not json")

    def test_json_that_is_not_a_list_is_damaged(self):
        self.history_file.write_text('{"id": "x"}', encoding="utf-8")
        self.assertEqual(self.get("/history").json()["file_state"], "damaged")
        self.assert_history_untouched(b'{"id": "x"}')

    def test_an_empty_file_is_damaged(self):
        self.history_file.write_bytes(b"")
        self.assertEqual(self.get("/history").json()["file_state"], "damaged")

    def test_invalid_utf8_is_damaged(self):
        self.history_file.write_bytes(b"\xff\xfe\x00[]")
        self.assertEqual(self.get("/history").json()["file_state"], "damaged")

    def test_a_valid_empty_list_is_a_healthy_empty_history(self):
        self.history_file.write_text("[]", encoding="utf-8")
        self.assertEqual(self.get("/history").json(),
                         {"file_state": "ok", "entries": [], "problems": [], "max_entries": 25})

    def test_unreadable_history_is_reported_not_hidden_and_a_brief_lock_is_retried(self):
        self.make_run(CALCULATOR)
        before = self.history_file.read_bytes()
        real = Path.read_text
        calls = []

        def locked_once(path, *args, **kwargs):
            if Path(path) == self.history_file and not calls:
                calls.append(1)
                raise PermissionError("locked for a moment")
            return real(path, *args, **kwargs)

        with mock.patch("aether_api.repository.time.sleep"), mock.patch.object(Path, "read_text", locked_once):
            self.assertEqual(self.get("/history").json()["file_state"], "ok")  # the retry succeeded
        self.assertEqual(calls, [1])

        def always_locked(path, *args, **kwargs):
            if Path(path) == self.history_file:
                raise PermissionError("locked")
            return real(path, *args, **kwargs)

        with mock.patch("aether_api.repository.time.sleep"), mock.patch.object(Path, "read_text", always_locked):
            body = self.get("/history").json()
        self.assertEqual((body["file_state"], body["entries"]), ("unreadable", []))
        self.assertEqual(self.history_file.read_bytes(), before)

    def test_unreadable_entries_are_listed_as_problems_while_good_ones_still_show(self):
        self.make_run(CALCULATOR)
        entries = json.loads(self.history_file.read_text(encoding="utf-8"))
        newer = json.loads(json.dumps(entries[0]))
        newer["id"], newer["schema_version"] = "from-the-future", 99
        broken = {"id": "broken", "schema_version": 1, "result": {"requirement": "x"}}
        raw = json.dumps([newer, broken] + entries, indent=2)
        self.history_file.write_text(raw, encoding="utf-8")

        body = self.get("/history").json()
        self.assertEqual(body["file_state"], "ok")
        self.assertEqual(len(body["entries"]), 1)
        self.assertEqual(len(body["problems"]), 2)
        self.assertIn("newer AETHER", body["problems"][0])
        self.assertIn("missing required field", body["problems"][1])
        self.assertEqual(self.history_file.read_text(encoding="utf-8"), raw, "skipped entries must stay in the file")

    def test_a_snapshot_of_a_damaged_history_is_an_error_not_a_404(self):
        self.history_file.write_text("nope", encoding="utf-8")
        error = self.assert_error(self.get("/history/anything"), 422, "history_damaged")
        self.assertIn("damaged", error["message"])

    def test_a_snapshot_of_an_unreadable_history_is_a_503(self):
        self.make_run(CALCULATOR)
        real = Path.read_text

        def locked(path, *args, **kwargs):
            if Path(path) == self.history_file:
                raise PermissionError("locked")
            return real(path, *args, **kwargs)

        with mock.patch("aether_api.repository.time.sleep"), mock.patch.object(Path, "read_text", locked):
            self.assert_error(self.get("/history/anything"), 503, "history_unavailable")


class HistorySnapshotTests(ApiTestCase):
    def test_a_snapshot_with_its_stored_run(self):
        context, entry = self.make_run(TODO_WITH_API)
        body = self.get(f"/history/{entry['id']}").json()
        self.assertEqual((body["source"], body["entry_id"], body["run_id"]), ("snapshot", entry["id"], context.run_id))
        self.assertEqual(body["context_state"], "ok")
        self.assertEqual(body["status"], "awaiting_approval")
        self.assertTrue(body["ok"])
        self.assertIsInstance(body["duration_ms"], float)  # run time comes only from a history snapshot
        self.assertIn("[Manager] Requirement received", "\n".join(body["manager_log"]))
        self.assertIsNotNone(body["events"])
        self.assertIsNone(body["stages"], "stages are only for snapshots without a stored run")
        self.assertEqual(body["actions"]["can_approve"], True)
        self.assertIsNone(body["actions"]["disabled_reason"])

    def test_a_snapshot_without_a_stored_run_uses_the_saved_stages(self):
        result, _ = start_project(CALCULATOR, store=None)
        entry = history.save_run(result)
        body = self.get(f"/history/{entry['id']}").json()
        self.assertEqual((body["run_id"], body["context_state"], body["status"], body["events"]), (None, "missing", None, None))
        self.assertEqual([s["agent"] for s in body["stages"]][:2], ["Manager", "Manager"])
        self.assertEqual(body["actions"], {"can_approve": False, "can_reject": False, "can_override": False,
                                           "disabled_reason": "This saved plan has no stored run, so it cannot be approved."})
        self.assertEqual([p["status"] for p in body["progress"]], ["done", "done", "pending", "pending"])

    def test_a_snapshot_whose_run_folder_is_gone_cannot_be_approved_and_says_why(self):
        context, entry = self.make_run(CALCULATOR)
        shutil.rmtree(self.run_dir(context.run_id))
        body = self.get(f"/history/{entry['id']}").json()
        self.assertEqual((body["context_state"], body["status"]), ("missing", None))
        self.assertIn("could not be loaded", body["actions"]["disabled_reason"])
        self.assertTrue(body["ok"], "the saved plan is still shown")

    def test_with_an_invalid_runs_folder_the_reason_is_always_given(self):
        _, entry = self.make_run(CALCULATOR)
        self.assertFalse(INVALID_RUNS_FOLDER.exists())
        with mock.patch.dict(os.environ, {"AETHER_RUNS_DIR": str(INVALID_RUNS_FOLDER)}):
            responses = [self.get(f"/history/{entry['id']}") for _ in range(2)]  # not a one-off message (K39)
        for response in responses:
            body = response.json()
            self.assertEqual(body["context_state"], "unreadable")
            self.assertFalse(body["actions"]["can_approve"])
            self.assertIn("cannot be saved", body["actions"]["disabled_reason"])
            self.assertIn("outside it", body["actions"]["disabled_reason"])
            self.assertIsNone(body["workspace"])
            self.assert_no_private_text(response)
        self.assertFalse(INVALID_RUNS_FOLDER.exists())

    def test_an_unknown_entry_id_is_a_404(self):
        self.make_run(CALCULATOR)
        self.assert_error(self.get("/history/not-an-entry"), 404, "history_entry_not_found")

    def test_an_entry_that_the_list_skipped_is_not_found(self):
        self.make_run(CALCULATOR)
        entries = json.loads(self.history_file.read_text(encoding="utf-8"))
        entries.append({"id": "future", "schema_version": 99, "result": {}})
        self.history_file.write_text(json.dumps(entries), encoding="utf-8")
        self.assert_error(self.get("/history/future"), 404, "history_entry_not_found")

    def test_reading_a_snapshot_does_not_change_history_or_the_run(self):
        context, entry = self.make_run(CALCULATOR, approve=True)
        files = {p: p.read_bytes() for p in [self.history_file, *self.run_dir(context.run_id).rglob("*")] if p.is_file()}
        self.get(f"/history/{entry['id']}")
        self.get("/history")
        self.assertEqual(files, {p: p.read_bytes() for p in files})


if __name__ == "__main__":
    unittest.main()
