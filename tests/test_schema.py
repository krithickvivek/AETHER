"""Tests for persistence schema versioning and backward-compatible loading (Task 003)."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from aether import schema
from aether.context import DONE, FAILED, Approval, FileRecord, ProjectContext, RunStore
from aether.models import Plan, PlanStep, TaskBrief
from aether.schema import SCHEMA_VERSION, SchemaError, upgrade
from aether_ui import history
from aether_ui.workflow import result_from_dict, result_to_dict, run_workflow


def legacy_context_dict() -> dict:
    """A run record exactly as saved before schema versioning existed (no schema_version key)."""
    return {
        "run_id": "abc123def456", "requirement": "Build a to-do website", "created_at": "2026-11-01T10:00:00",
        "status": "completed",
        "brief": {"requirement": "Build a to-do website", "features": ["web_ui"]},
        "plan": {"requirement": "Build a to-do website", "planner_name": "RuleBasedPlanner",
                 "steps": [{"phase": "Design", "description": "Sketch the pages"}]},
        "project_type": "todo_web",
        "files": [{"path": "server.py", "author": "Developer", "sha256": "ab12", "size": 10, "covers": [1]}],
        "approvals": [{"checkpoint": "A", "decision": "approved", "time": "2026-11-01T10:01:00", "note": "",
                       "choice": ""}],
        "events": [{"time": "2026-11-01T10:00:00", "agent": "Manager", "action": "Understand requirement",
                    "status": "done", "detail": "1 feature detected", "duration_ms": None}],
        "notes": {"developer_template": "To-do web app"},
    }


def sample_context() -> ProjectContext:
    context = ProjectContext.new("Build a to-do website")
    context.brief = TaskBrief("Build a to-do website", ["web_ui"])
    context.plan = Plan("Build a to-do website", "RuleBasedPlanner", [PlanStep("Design", "Sketch the pages")])
    context.files.append(FileRecord("app.py", "Developer", "abc123", 42, [1]))
    context.approvals.append(Approval("A", "approved", "2026-11-01T10:00:00", "ok", "todo_web"))
    context.notes["k"] = "v"
    context.record("Manager", "Understand requirement", DONE, "1 feature detected")
    context.record("Planner", "Build development plan", FAILED, "model unavailable", duration_ms=12.5)
    return context


def legacy_history_entry() -> dict:
    """A history entry as saved before schema versioning existed."""
    entry = {"id": "e" * 32, "result": result_to_dict(run_workflow("A website with login"))}
    del entry["result"][schema.VERSION_KEY]
    return entry


class UpgradeTests(unittest.TestCase):
    def test_unversioned_record_is_version_zero_and_is_upgraded(self):
        upgraded = upgrade({"a": 1}, schema.KIND_CONTEXT)
        self.assertEqual(upgraded, {"a": 1, "schema_version": SCHEMA_VERSION})

    def test_upgrade_does_not_modify_its_input(self):
        original = legacy_context_dict()
        snapshot = copy.deepcopy(original)
        upgrade(original, schema.KIND_CONTEXT)
        self.assertEqual(original, snapshot)

    def test_current_version_record_is_accepted(self):
        record = {"schema_version": SCHEMA_VERSION, "x": 1}
        self.assertEqual(upgrade(record, schema.KIND_CONTEXT), record)

    def test_newer_version_is_refused_with_a_clear_message(self):
        with self.assertRaises(SchemaError) as caught:
            upgrade({"schema_version": SCHEMA_VERSION + 1}, schema.KIND_CONTEXT, "Run record")
        self.assertIn("newer AETHER", str(caught.exception))
        self.assertIn(str(SCHEMA_VERSION + 1), str(caught.exception))

    def test_invalid_version_values_are_refused(self):
        for bad in ("1", -1, 1.5, True, None, [1]):
            with self.subTest(version=bad), self.assertRaises(SchemaError):
                upgrade({"schema_version": bad}, schema.KIND_CONTEXT)

    def test_non_object_record_is_refused(self):
        for bad in ([], "text", 5, None):
            with self.subTest(record=bad), self.assertRaises(SchemaError):
                upgrade(bad, schema.KIND_CONTEXT)

    def test_every_kind_has_a_migration_path_from_legacy_to_current(self):
        """Guards a future version bump: raising SCHEMA_VERSION without adding migrations fails here."""
        for kind in schema.KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(upgrade({}, kind)["schema_version"], SCHEMA_VERSION)
                for version in range(schema.LEGACY_VERSION, SCHEMA_VERSION):
                    self.assertIn((kind, version), schema.MIGRATIONS)

    def test_missing_migration_is_reported(self):
        saved = dict(schema.MIGRATIONS)
        self.addCleanup(lambda: (schema.MIGRATIONS.clear(), schema.MIGRATIONS.update(saved)))
        del schema.MIGRATIONS[(schema.KIND_CONTEXT, 0)]
        with self.assertRaisesRegex(SchemaError, "no migration"):
            upgrade({}, schema.KIND_CONTEXT)


class ContextLoadingTests(unittest.TestCase):
    def test_legacy_record_loads_with_all_data(self):
        context = ProjectContext.from_dict(legacy_context_dict())
        self.assertEqual(context.run_id, "abc123def456")
        self.assertEqual(context.status, "completed")
        self.assertEqual(context.brief.features, ["web_ui"])
        self.assertEqual(context.plan.steps, [PlanStep("Design", "Sketch the pages")])
        self.assertEqual(context.project_type, "todo_web")
        self.assertEqual(context.files, [FileRecord("server.py", "Developer", "ab12", 10, [1])])
        self.assertEqual(context.approvals[0].decision, "approved")
        self.assertEqual(context.events[0].agent, "Manager")
        self.assertEqual(context.notes, {"developer_template": "To-do web app"})
        self.assertEqual(context.load_warnings, [])

    def test_saved_records_carry_the_current_version(self):
        data = sample_context().to_dict()
        self.assertEqual(data["schema_version"], SCHEMA_VERSION)
        self.assertEqual(next(iter(data)), "schema_version")  # first key, easy to spot in the file
        self.assertEqual(ProjectContext.from_dict(data).to_dict(), data)

    def test_round_trip_keeps_everything(self):
        context = sample_context()
        restored = ProjectContext.from_dict(json.loads(json.dumps(context.to_dict())))
        self.assertEqual(restored, context)

    def test_legacy_record_resaved_as_current_version_keeps_its_data(self):
        legacy = legacy_context_dict()
        resaved = ProjectContext.from_dict(legacy).to_dict()
        self.assertEqual(resaved.pop("schema_version"), SCHEMA_VERSION)
        self.assertEqual(resaved, legacy)

    def test_missing_optional_fields_get_safe_defaults(self):
        context = ProjectContext.from_dict({"run_id": "abc", "requirement": "x", "created_at": "t"})
        self.assertEqual(context.status, "running")
        self.assertIsNone(context.brief)
        self.assertIsNone(context.plan)
        self.assertIsNone(context.project_type)
        self.assertEqual((context.files, context.approvals, context.events, context.notes), ([], [], [], {}))

    def test_missing_optional_fields_inside_items_get_defaults(self):
        data = {"run_id": "abc", "requirement": "x", "created_at": "t",
                "files": [{"path": "a.py", "author": "Developer", "sha256": "z", "size": 1}],
                "events": [{"time": "t", "agent": "A", "action": "b", "status": "done"}],
                "approvals": [{"checkpoint": "A", "decision": "approved", "time": "t"}]}
        context = ProjectContext.from_dict(data)
        self.assertEqual(context.files[0].covers, [])
        self.assertEqual((context.events[0].detail, context.events[0].duration_ms), ("", None))
        self.assertEqual((context.approvals[0].note, context.approvals[0].choice), ("", ""))

    def test_null_optional_collections_are_treated_as_empty(self):
        data = {"run_id": "abc", "requirement": "x", "created_at": "t", "files": None, "events": None,
                "notes": None}
        context = ProjectContext.from_dict(data)
        self.assertEqual((context.files, context.events, context.notes), ([], [], {}))

    def test_unknown_top_level_fields_are_kept_reported_and_saved_back(self):
        data = {**legacy_context_dict(), "schema_version": SCHEMA_VERSION, "future_metric": {"score": 3}}
        context = ProjectContext.from_dict(data)
        self.assertEqual(context.unknown_fields, {"future_metric": {"score": 3}})
        self.assertTrue(any("future_metric" in w for w in context.load_warnings))
        self.assertEqual(context.to_dict()["future_metric"], {"score": 3})
        self.assertEqual(context.to_dict(), data)

    def test_unknown_fields_never_override_real_fields_or_affect_equality(self):
        data = {**legacy_context_dict(), "extra": 1}
        self.assertEqual(ProjectContext.from_dict(data), ProjectContext.from_dict(legacy_context_dict()))

    def test_unknown_nested_fields_are_ignored_with_a_warning(self):
        data = legacy_context_dict()
        data["files"][0]["mode"] = "rw"
        data["plan"]["steps"][0]["owner"] = "x"
        context = ProjectContext.from_dict(data)
        self.assertEqual(context.files[0].path, "server.py")
        self.assertEqual(len([w for w in context.load_warnings if "unknown field" in w]), 2)

    def test_unrecognised_status_is_kept_with_a_warning(self):
        context = ProjectContext.from_dict({**legacy_context_dict(), "status": "paused"})
        self.assertEqual(context.status, "paused")
        self.assertTrue(any("status" in w for w in context.load_warnings))

    def test_unknown_schema_version_is_refused(self):
        with self.assertRaisesRegex(SchemaError, "newer AETHER"):
            ProjectContext.from_dict({**legacy_context_dict(), "schema_version": SCHEMA_VERSION + 5})

    def test_missing_essential_fields_are_named(self):
        for name in ("run_id", "requirement", "created_at"):
            data = legacy_context_dict()
            del data[name]
            with self.subTest(missing=name), self.assertRaisesRegex(SchemaError, name):
                ProjectContext.from_dict(data)

    def test_invalid_essential_values_are_refused(self):
        cases = [("run_id", 5), ("run_id", "  "), ("requirement", None), ("created_at", 1.5), ("status", 3)]
        for name, value in cases:
            with self.subTest(field=name, value=value), self.assertRaises(SchemaError):
                ProjectContext.from_dict({**legacy_context_dict(), name: value})

    def test_malformed_parts_are_refused_with_their_location(self):
        def change(path, value):
            data = legacy_context_dict()
            target = data
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            return data

        cases = {
            "events[0]": change(["events"], ["not an object"]),
            "files[0]": change(["files", 0, "size"], "big"),
            "plan.steps[0]": change(["plan", "steps", 0, "phase"], 7),
            "brief": change(["brief"], "text"),
            "approvals[0]": change(["approvals"], [{"checkpoint": "A"}]),
            "notes": change(["notes"], ["a"]),
            "files": change(["files"], {"path": "x"}),
            "covers": change(["files", 0, "covers"], ["one"]),
        }
        for location, data in cases.items():
            with self.subTest(location=location), self.assertRaisesRegex(SchemaError, location.split("[")[0]):
                ProjectContext.from_dict(data)

    def test_a_number_is_not_accepted_where_a_flag_like_value_is_wrong(self):
        data = legacy_context_dict()
        data["files"][0]["size"] = True  # true/false must not pass as a whole number
        with self.assertRaises(SchemaError):
            ProjectContext.from_dict(data)

    def test_schema_error_is_a_value_error(self):
        """app.py already catches ValueError when opening a saved run."""
        self.assertTrue(issubclass(SchemaError, ValueError))


class RunStorePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RunStore(Path(self.temp.name))

    def write_context(self, run_id: str, data: dict) -> Path:
        folder = self.store.run_dir(run_id)
        folder.mkdir(parents=True)
        (folder / "context.json").write_text(json.dumps(data), encoding="utf-8")
        return folder

    def test_saved_file_has_the_version_and_the_same_layout_as_before(self):
        context = sample_context()
        folder = self.store.save(context)
        saved = json.loads((folder / "context.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["schema_version"], SCHEMA_VERSION)
        legacy_keys = {"run_id", "requirement", "created_at", "status", "brief", "plan", "project_type", "files",
                       "approvals", "events", "notes"}
        self.assertEqual(set(saved), legacy_keys | {"schema_version"})
        for line in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines():
            self.assertNotIn("schema_version", json.loads(line))  # events.jsonl keeps its existing format

    def test_unversioned_file_on_disk_loads_and_is_upgraded_on_next_save(self):
        legacy = legacy_context_dict()
        folder = self.write_context(legacy["run_id"], legacy)
        context = self.store.load(legacy["run_id"])
        self.assertEqual(context.plan.steps[0].description, "Sketch the pages")
        self.store.save(context)
        saved = json.loads((folder / "context.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["schema_version"], SCHEMA_VERSION)
        self.assertEqual(self.store.load(legacy["run_id"]), context)

    def test_save_and_load_round_trip(self):
        context = sample_context()
        self.store.save(context)
        self.assertEqual(self.store.load(context.run_id), context)

    def test_newer_file_is_refused_and_left_untouched(self):
        data = {**legacy_context_dict(), "schema_version": SCHEMA_VERSION + 1}
        folder = self.write_context(data["run_id"], data)
        before = (folder / "context.json").read_text(encoding="utf-8")
        with self.assertRaisesRegex(SchemaError, "abc123def456.*newer AETHER"):
            self.store.load(data["run_id"])
        self.assertEqual((folder / "context.json").read_text(encoding="utf-8"), before)

    def test_damaged_json_gives_a_clear_error_naming_the_run(self):
        folder = self.store.run_dir("brokenrun")
        folder.mkdir(parents=True)
        (folder / "context.json").write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(SchemaError, "brokenrun.*not valid JSON"):
            self.store.load("brokenrun")

    def test_invalid_essential_data_names_the_run_and_the_field(self):
        data = legacy_context_dict()
        del data["requirement"]
        self.write_context(data["run_id"], data)
        with self.assertRaisesRegex(SchemaError, "abc123def456.*requirement"):
            self.store.load(data["run_id"])

    def test_missing_file_is_still_an_os_error(self):
        with self.assertRaises(OSError):
            self.store.load("doesnotexist")

    def test_folder_and_record_id_mismatch_is_reported(self):
        data = legacy_context_dict()
        self.write_context("otherfolder", data)
        self.assertTrue(any("otherfolder" in w for w in self.store.load("otherfolder").load_warnings))

    def test_unknown_fields_survive_a_save_load_cycle(self):
        data = {**legacy_context_dict(), "future_metric": [1, 2]}
        self.write_context(data["run_id"], data)
        context = self.store.load(data["run_id"])
        self.store.save(context)
        saved = json.loads((self.store.run_dir(data["run_id"]) / "context.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["future_metric"], [1, 2])

    def test_events_file_loads_line_by_line(self):
        context = sample_context()
        self.store.save(context)
        self.assertEqual(self.store.load_events(context.run_id), context.events)

    def test_events_file_without_optional_fields_and_with_blank_lines_loads(self):
        folder = self.store.run_dir("evrun")
        folder.mkdir(parents=True)
        (folder / "events.jsonl").write_text(
            '{"time": "t", "agent": "A", "action": "b", "status": "done"}\n\n', encoding="utf-8")
        events = self.store.load_events("evrun")
        self.assertEqual((len(events), events[0].detail), (1, ""))

    def test_bad_events_line_is_reported_with_its_line_number(self):
        folder = self.store.run_dir("evrun")
        folder.mkdir(parents=True)
        good = '{"time": "t", "agent": "A", "action": "b", "status": "done"}'
        (folder / "events.jsonl").write_text(f"{good}\n{{oops\n", encoding="utf-8")
        with self.assertRaisesRegex(SchemaError, "line 2"):
            self.store.load_events("evrun")
        (folder / "events.jsonl").write_text(f'{good}\n{{"agent": "A"}}\n', encoding="utf-8")
        with self.assertRaisesRegex(SchemaError, "line 2.*missing required"):
            self.store.load_events("evrun")


class RunResultAndHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "history.json"

    def write(self, entries) -> None:
        self.path.write_text(json.dumps(entries), encoding="utf-8")

    def test_result_dict_carries_the_version_and_round_trips(self):
        result = run_workflow("A website with login and a database")
        data = result_to_dict(result)
        self.assertEqual(data["schema_version"], SCHEMA_VERSION)
        self.assertEqual(result_from_dict(json.loads(json.dumps(data))), result)

    def test_unversioned_result_loads(self):
        result = run_workflow("A website with login")
        data = result_to_dict(result)
        del data["schema_version"]
        self.assertEqual(result_from_dict(data), result)

    def test_result_from_before_fallback_and_run_id_existed_loads(self):
        data = result_to_dict(run_workflow("A website"))
        for name in ("schema_version", "fallback_reason", "run_id"):
            del data[name]
        restored = result_from_dict(data)
        self.assertIsNone(restored.fallback_reason)
        self.assertIsNone(restored.run_id)

    def test_result_with_unknown_field_loads_and_reports_it(self):
        data = {**result_to_dict(run_workflow("A website")), "future": 1}
        warnings = []
        result_from_dict(data, warnings)
        self.assertTrue(any("future" in w for w in warnings))

    def test_result_refusals(self):
        good = result_to_dict(run_workflow("A website"))
        for name in ("requirement", "started_at", "duration_ms", "stages", "log"):
            data = dict(good)
            del data[name]
            with self.subTest(missing=name), self.assertRaisesRegex(SchemaError, name):
                result_from_dict(data)
        with self.assertRaisesRegex(SchemaError, "newer AETHER"):
            result_from_dict({**good, "schema_version": SCHEMA_VERSION + 1})
        with self.assertRaises(SchemaError):
            result_from_dict({**good, "stages": [{"agent": "Manager"}]})
        with self.assertRaises(SchemaError):
            result_from_dict({**good, "log": [1]})

    def test_new_history_entries_are_versioned(self):
        entry = history.save_run(run_workflow("A website"), self.path)
        self.assertEqual(entry["schema_version"], SCHEMA_VERSION)
        self.assertEqual(entry["result"]["schema_version"], SCHEMA_VERSION)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(set(saved[0]), {"schema_version", "id", "result"})

    def test_legacy_history_entries_load_and_open(self):
        legacy = legacy_history_entry()
        self.write([legacy])
        entries = history.load_history(self.path)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], legacy["id"])
        self.assertEqual(history.entry_to_result(entries[0]).requirement, "A website with login")

    def test_saving_keeps_legacy_entries_exactly_as_they_were(self):
        legacy = legacy_history_entry()
        self.write([legacy])
        history.save_run(run_workflow("Another website"), self.path)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[1], legacy)  # not rewritten, not stamped

    def test_unreadable_entries_are_skipped_reported_and_not_deleted(self):
        good = legacy_history_entry()
        newer = {**legacy_history_entry(), "schema_version": SCHEMA_VERSION + 1}
        broken = {"id": "x" * 32, "result": {"requirement": "no other fields"}}
        junk = "just text"
        self.write([newer, broken, junk, good])
        entries, problems = history.load_history_report(self.path)
        self.assertEqual([e["id"] for e in entries], [good["id"]])
        self.assertEqual(len(problems), 3)
        self.assertIn("newer AETHER", problems[0])
        self.assertIn("missing required", problems[1])
        history.save_run(run_workflow("A new one"), self.path)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved[1:], [newer, broken, junk, good])  # nothing lost

    def test_damaged_file_is_copied_aside_before_being_replaced(self):
        self.path.write_text("not json at all", encoding="utf-8")
        self.assertEqual(history.load_history(self.path), [])
        history.save_run(run_workflow("A website"), self.path)
        backups = list(self.path.parent.glob("history.json.damaged-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "not json at all")
        self.assertEqual(len(history.load_history(self.path)), 1)

    def test_a_file_that_is_not_a_list_is_treated_as_damaged(self):
        self.write({"entries": []})
        self.assertEqual(history.load_history(self.path), [])
        history.save_run(run_workflow("A website"), self.path)
        self.assertEqual(len(list(self.path.parent.glob("history.json.damaged-*"))), 1)

    def test_entry_missing_id_or_result_is_unreadable(self):
        self.write([{"result": result_to_dict(run_workflow("A website"))}, {"id": "a"}])
        entries, problems = history.load_history_report(self.path)
        self.assertEqual((entries, len(problems)), ([], 2))
        with self.assertRaises(SchemaError):
            history.entry_to_result({"id": "a"})

    def test_history_round_trip_keeps_the_plan(self):
        result = run_workflow("A website with login and a database")
        history.save_run(result, self.path)
        reopened = history.entry_to_result(history.load_history(self.path)[0])
        self.assertEqual(reopened, result)


if __name__ == "__main__":
    unittest.main()
