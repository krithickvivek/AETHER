"""Tests for the Workspace: safe, limited file writing inside one project folder."""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aether import workspace
from aether.workspace import Workspace, WorkspaceError


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.outside = Path(self.temp.name)
        self.ws = Workspace(self.outside / "run" / "workspace")

    def test_writes_file_and_records_it(self):
        record = self.ws.write_text("src/app.py", "print('hi')\n", author="Developer")
        self.assertEqual(record.path, "src/app.py")
        self.assertEqual(record.author, "Developer")
        self.assertEqual(record.sha256, hashlib.sha256(b"print('hi')\n").hexdigest())
        self.assertEqual(self.ws.read_text("src/app.py"), "print('hi')\n")
        self.assertEqual(self.ws.list_files(), ["src/app.py"])

    def test_backslash_paths_are_stored_with_forward_slashes(self):
        self.assertEqual(self.ws.write_text("tests\\test_app.py", "", "Tester").path, "tests/test_app.py")

    def test_refuses_paths_that_leave_the_folder(self):
        for bad in ["../evil.py", "src/../../evil.py", "..\\evil.py", "/etc/evil.py", "\\evil.py",
                    "C:\\evil.py", "C:evil.py", "\\\\server\\share\\evil.py", str(self.outside / "evil.py")]:
            with self.subTest(path=bad), self.assertRaises(WorkspaceError):
                self.ws.write_text(bad, "x", "Developer")
        self.assertFalse((self.outside / "evil.py").exists())
        self.assertFalse((self.outside / "run" / "evil.py").exists())

    def test_refuses_empty_or_odd_paths(self):
        for bad in ["", "   ", "a\0.py", None]:
            with self.subTest(path=bad), self.assertRaises(WorkspaceError):
                self.ws.write_text(bad, "x", "Developer")

    def test_refuses_unexpected_file_types(self):
        for bad in ["run.bat", "script.ps1", "tool.exe", "Makefile", "app.py.sh"]:
            with self.subTest(path=bad), self.assertRaises(WorkspaceError):
                self.ws.write_text(bad, "x", "Developer")

    def test_refuses_oversized_files(self):
        with self.assertRaisesRegex(WorkspaceError, "limit"):
            self.ws.write_text("big.txt", "x" * (workspace.MAX_FILE_BYTES + 1), "Developer")
        self.assertEqual(self.ws.list_files(), [])

    def test_limits_number_of_files_but_allows_overwrite(self):
        with mock.patch.object(workspace, "MAX_FILES", 2):
            self.ws.write_text("a.py", "1", "Developer")
            self.ws.write_text("b.py", "2", "Developer")
            self.ws.write_text("a.py", "updated", "Developer")  # replacing an existing file is fine
            with self.assertRaisesRegex(WorkspaceError, "maximum"):
                self.ws.write_text("c.py", "3", "Developer")
        self.assertEqual(self.ws.read_text("a.py"), "updated")

    def test_refuses_link_that_points_outside(self):
        link = self.ws.root / "linked"
        try:
            os.symlink(self.outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            try:  # Windows without admin rights: a directory junction is the same kind of escape
                import _winapi
                _winapi.CreateJunction(str(self.outside), str(link))
            except (ImportError, OSError):
                self.skipTest("This computer does not allow creating folder links.")
        with self.assertRaisesRegex(WorkspaceError, "outside"):
            self.ws.write_text("linked/evil.py", "x", "Developer")

    def test_no_temporary_files_left_behind(self):
        self.ws.write_text("notes.md", "# Notes", "Documentation")
        self.assertEqual([p.name for p in self.ws.root.iterdir()], ["notes.md"])


if __name__ == "__main__":
    unittest.main()
