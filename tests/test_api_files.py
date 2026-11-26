"""Generated files and the plan download: GET /runs/{id}/files, /files/content?path=, /plan.md."""

import hashlib
import unittest
from unittest import mock

try:
    from api_support import CALCULATOR, CALCULATOR_FILES, TODO, ApiTestCase
except ImportError as error:
    if "requirements-api.txt" in str(error):
        raise
    from tests.api_support import CALCULATOR, CALCULATOR_FILES, TODO, ApiTestCase

from aether import workspace
from aether.context import RunStore
from aether_ui.workflow import decide_plan


class FileListTests(ApiTestCase):
    def test_the_recorded_files_of_a_completed_run(self):
        context, _ = self.make_run(CALCULATOR, approve=True)
        response = self.get(f"/runs/{context.run_id}/files")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["run_id"], context.run_id)
        self.assertEqual(sorted(f["path"] for f in body["files"]), CALCULATOR_FILES)
        calculator = next(f for f in body["files"] if f["path"] == "calculator.py")
        record = next(r for r in context.files if r.path == "calculator.py")
        self.assertEqual((calculator["size"], calculator["sha256"], calculator["author"], calculator["language"]),
                         (record.size, record.sha256, "Developer", "python"))
        self.assertEqual(calculator["covers"], record.covers)
        self.assertTrue(body["workspace"]["exists"])
        self.assert_no_private_text(response)

    def test_a_run_that_has_no_files_yet_has_an_empty_list(self):
        context, _ = self.make_run(CALCULATOR)
        body = self.get(f"/runs/{context.run_id}/files").json()
        self.assertEqual(body["files"], [])
        self.assertFalse(body["workspace"]["exists"])

    def test_a_missing_run_is_a_404(self):
        self.assert_error(self.get("/runs/abcdef123456/files"), 404, "run_not_found")


class FileContentTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.context, _ = self.make_run(CALCULATOR, approve=True)
        self.workspace_dir = self.run_dir(self.context.run_id) / "workspace"

    def content(self, path: str):
        return self.get(f"/runs/{self.context.run_id}/files/content", params={"path": path})

    def test_the_text_of_a_file(self):
        response = self.content("calculator.py")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        on_disk = (self.workspace_dir / "calculator.py").read_text(encoding="utf-8")
        self.assertEqual(body["content"], on_disk)
        self.assertEqual((body["path"], body["language"], body["missing"], body["changed"]),
                         ("calculator.py", "python", False, False))
        self.assertEqual(body["current_sha256"], body["sha256"])
        self.assertEqual(body["sha256"], hashlib.sha256(on_disk.encode("utf-8")).hexdigest())
        self.assertEqual(body["size"], len(on_disk.encode("utf-8")))

    def test_a_file_in_a_subfolder_and_a_backslash_path(self):
        self.assertEqual(self.content("tests/test_calculator.py").status_code, 200)
        self.assertEqual(self.content("tests\\test_calculator.py").json()["path"], "tests/test_calculator.py")

    def test_every_recorded_file_can_be_read(self):
        for path in CALCULATOR_FILES:
            with self.subTest(path=path):
                body = self.content(path).json()
                self.assertFalse(body["missing"])
                self.assertFalse(body["changed"])

    def test_a_file_changed_since_the_developer_wrote_it_is_flagged(self):
        (self.workspace_dir / "calculator.py").write_text("print('edited')\n", encoding="utf-8")
        body = self.content("calculator.py").json()
        self.assertTrue(body["changed"])
        self.assertEqual(body["content"], "print('edited')\n")
        self.assertNotEqual(body["current_sha256"], body["sha256"])

    def test_a_file_removed_from_disk_is_reported_as_missing_not_an_error(self):
        (self.workspace_dir / "calculator.py").unlink()
        response = self.content("calculator.py")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["missing"], body["content"], body["current_sha256"], body["changed"]),
                         (True, None, None, False))

    def test_a_workspace_folder_that_is_gone_is_reported_as_missing_and_not_recreated(self):
        import shutil
        shutil.rmtree(self.workspace_dir)
        body = self.content("calculator.py").json()
        self.assertTrue(body["missing"])
        self.assertFalse(self.workspace_dir.exists(), "reading must not recreate the workspace folder")
        self.assertEqual(self.get(f"/runs/{self.context.run_id}/files").json()["workspace"]["exists"], False)

    def test_a_file_replaced_by_a_folder_is_missing(self):
        target = self.workspace_dir / "README.md"
        target.unlink()
        target.mkdir()
        self.assertTrue(self.content("README.md").json()["missing"])

    def test_a_file_that_is_no_longer_text_is_a_422(self):
        (self.workspace_dir / "calculator.py").write_bytes(b"\xff\xfe\x00\x80")
        self.assert_error(self.content("calculator.py"), 422, "file_not_text")

    def test_paths_that_are_not_safe_are_a_422(self):
        for path in ("../context.json", "..\\context.json", "tests/../../context.json", "/etc/passwd",
                     "C:\\Windows\\win.ini", "C:/Windows/win.ini", "\\\\server\\share\\x.py", "program.exe",
                     "calculator", "tests", "a\x00b.py", ".."):
            with self.subTest(path=path):
                error = self.assert_error(self.content(path), 422, "invalid_path")
                self.assertIn("must be relative", error["message"])
                self.assertNotIn("win.ini", error["message"], "the requested path is not echoed back")

    def test_a_safe_path_that_is_not_one_of_the_run_s_files_is_a_404(self):
        for path in ("other.py", "tests/test_other.py", "notes.txt"):
            with self.subTest(path=path):
                self.assert_error(self.content(path), 404, "file_not_found")

    def test_a_file_that_exists_on_disk_but_was_not_recorded_cannot_be_read(self):
        (self.workspace_dir / "secret.py").write_text("x = 1\n", encoding="utf-8")
        self.assert_error(self.content("secret.py"), 404, "file_not_found")

    def test_the_path_parameter_is_required(self):
        error = self.assert_error(self.get(f"/runs/{self.context.run_id}/files/content"), 422, "invalid_request")
        self.assertEqual(error["details"][0]["location"], "query.path")
        self.assert_error(self.content(""), 422, "invalid_request")
        self.assert_error(self.content("a" * 501), 422, "invalid_request")

    def test_the_invalid_request_error_does_not_echo_the_input(self):
        response = self.content("a" * 501)
        self.assertNotIn("a" * 100, response.text)

    def test_a_missing_run_is_a_404(self):
        self.assert_error(self.get("/runs/abcdef123456/files/content", params={"path": "calculator.py"}), 404,
                          "run_not_found")

    def test_a_folder_or_file_that_cannot_be_inspected_is_a_503_not_a_crash(self):
        from pathlib import Path
        real_is_dir, real_is_file = Path.is_dir, Path.is_file

        def locked_dir(path):
            if path.name == "workspace":
                raise PermissionError("access denied")
            return real_is_dir(path)

        def locked_file(path):
            if path.name == "calculator.py":
                raise PermissionError("access denied")
            return real_is_file(path)

        with mock.patch.object(Path, "is_dir", locked_dir):
            self.assert_error(self.content("calculator.py"), 503, "storage_unavailable")
        with mock.patch.object(Path, "is_file", locked_file):
            self.assert_error(self.content("calculator.py"), 503, "storage_unavailable")

    def test_a_run_whose_developer_failed_has_no_readable_files(self):
        failed, _ = self.make_run(CALCULATOR)
        with mock.patch.object(workspace, "MAX_FILE_BYTES", 10):
            decide_plan(failed, approved=True, store=RunStore())
        self.assert_error(self.get(f"/runs/{failed.run_id}/files/content", params={"path": "calculator.py"}), 404,
                          "file_not_found")


class PlanMarkdownTests(ApiTestCase):
    def test_the_plan_as_markdown(self):
        context, _ = self.make_run(TODO)
        response = self.get(f"/runs/{context.run_id}/plan.md")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/markdown"))
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertIn("aether_plan.md", response.headers["content-disposition"])
        self.assertTrue(response.text.startswith("# Development plan"))
        self.assertIn(f"**Requirement:** {TODO}", response.text)
        self.assertIn("## Implementation", response.text)
        self.assertIn("_Generated by AETHER (RuleBasedPlanner, rule-based)._", response.text)

    def test_it_matches_the_text_the_streamlit_download_uses(self):
        from aether_ui.workflow import plan_to_markdown
        context, _ = self.make_run(CALCULATOR)
        self.assertEqual(self.get(f"/runs/{context.run_id}/plan.md").text, plan_to_markdown(context.plan))

    def test_a_run_without_a_plan_is_a_404(self):
        context, _ = self.make_run("   ")
        self.assert_error(self.get(f"/runs/{context.run_id}/plan.md"), 404, "run_not_found")

    def test_a_missing_run_is_a_404(self):
        self.assert_error(self.get("/runs/abcdef123456/plan.md"), 404, "run_not_found")


if __name__ == "__main__":
    unittest.main()
