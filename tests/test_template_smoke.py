"""Smoke tests for the generated project templates (Task 006, K15).

AETHER's other tests only read and compile the template source. These tests generate each template the
way the Developer agent does, into a temporary folder, and then check that what was written is a working
project: every expected file is there, every Python file compiles, and the project's OWN tests pass when
run in a separate Python process.

What was inspected before these tests were written (the templates are fixed, so this is safe to run):
  * The three templates use only the Python standard library (no dependencies to install).
  * Their own tests use in-memory SQLite databases and call the application objects directly. They open no
    sockets, read no files outside the project and start no server.
  * server.py (to-do and library) only starts a server under `if __name__ == "__main__"`, so these tests
    never run it. They only import it, which has no side effects.
  * calculator.py is a pure command-line calculator with no I/O besides printing.

Safety rules the helper below enforces: the current Python interpreter in isolated mode (-I) with byte-code
writing off (-B), a stripped environment, the temporary project as the working folder, a timeout, byte-code writing off, and a guard that
makes any attempt to open a network connection or socket fail loudly.
"""

import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from aether.agents import PlannerAgent
from aether.context import COMPLETED, ProjectContext, RunStore
from aether.developer import DeveloperAgent
from aether.manager import ManagerAgent
from aether.planner import RuleBasedPlanner
from aether.templates import TEMPLATES
from aether.workspace import Workspace

try:
    import sqlite3  # noqa: F401  (the database templates need it)
    SQLITE_MISSING = ""
except ImportError as error:  # pragma: no cover - depends on how Python was built
    SQLITE_MISSING = f"Python's sqlite3 module is not available ({error})"

ROOT = Path(__file__).resolve().parent.parent
TEST_SUITE_TIMEOUT = 120   # seconds for a generated project's whole test suite
SHORT_TIMEOUT = 30         # seconds for a single command or import

REQUIREMENTS = {
    "calculator": "A command-line calculator for basic arithmetic",
    "todo_web": "Build a to-do website where users can log in and save their tasks",
    "library_api": "Create a REST API for a library that stores books and members in a database",
}

# What each template must produce. Written out by hand (not read from the template definitions) so that a
# template that silently loses a file is noticed.
EXPECTED_FILES = {
    "calculator": {"README.md", "calculator.py", "tests/__init__.py", "tests/test_calculator.py"},
    "todo_web": {
        "README.md", "server.py",
        "todo_app/__init__.py", "todo_app/auth.py", "todo_app/db.py", "todo_app/schema.sql",
        "todo_app/tasks.py", "todo_app/web.py", "todo_app/static/style.css",
        "tests/__init__.py", "tests/test_auth.py", "tests/test_tasks.py", "tests/test_web.py",
    },
    "library_api": {
        "README.md", "server.py",
        "library_api/__init__.py", "library_api/api.py", "library_api/db.py", "library_api/schema.sql",
        "library_api/store.py",
        "tests/__init__.py", "tests/test_api.py", "tests/test_store.py",
    },
}
ENTRY_POINT = {"calculator": "calculator.py", "todo_web": "server.py", "library_api": "server.py"}
NEEDS_SQLITE = {"calculator": False, "todo_web": True, "library_api": True}
FORBIDDEN_FILES = {"requirements.txt", "setup.py", "pyproject.toml", "Pipfile", "package.json"}
NETWORK_MODULES = {"socket", "urllib.request", "http.client", "requests", "ftplib", "smtplib", "telnetlib",
                   "subprocess", "ctypes"}

# Runs inside the child Python process, before anything of the generated project: any attempt to connect,
# listen or resolve a name raises, so a generated test cannot reach the network even by accident.
GUARD = r'''
import os, runpy, socket, sys

def _blocked(*args, **kwargs):
    raise OSError("network access is blocked while AETHER smoke-tests generated code")

for _name in ("connect", "connect_ex", "bind", "listen", "sendto"):
    setattr(socket.socket, _name, _blocked)
for _name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex"):
    setattr(socket, _name, _blocked)

_mode, *_rest = sys.argv[1:]
if _mode == "-m":                      # python -m <module> <args>
    _module, *_args = _rest
    sys.argv = [_module, *_args]
    runpy.run_module(_module, run_name="__main__", alter_sys=True)
elif _mode == "-c":                    # run a snippet with the project folder importable
    sys.path.insert(0, os.getcwd())
    exec(compile(_rest[0], "<smoke-test>", "exec"))
else:                                  # python <script> <args>
    sys.argv = [_mode, *_rest]
    runpy.run_path(_mode, run_name="__main__")
'''

ENV_KEEP = ("PATH", "SYSTEMROOT", "SystemDrive", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL")


@dataclass
class Outcome:
    """What happened when a command ran in a child process."""

    command: list
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def succeeded(self) -> bool:
        return not self.timed_out and self.returncode == 0

    @property
    def tests_run(self) -> int | None:
        found = re.search(r"^Ran (\d+) tests? in", self.stderr, re.MULTILINE)
        return int(found.group(1)) if found else None

    @property
    def tests_passed(self) -> bool:
        """True only if the child process ended normally AND unittest said OK AND at least one test ran."""
        return (self.succeeded and bool(re.search(r"^OK\b", self.stderr, re.MULTILINE))
                and (self.tests_run or 0) > 0)


def child_environment() -> dict:
    env = {key: os.environ[key] for key in ENV_KEEP if key in os.environ}
    env["NO_PROXY"] = "*"  # belt and braces: even a library that honoured proxies would not look for one
    return env


def run_python(folder: Path, args: list[str], timeout: float, guard: str = GUARD) -> Outcome:
    """Run the CURRENT Python in isolated mode inside `folder`. Never raises for failure or timeout."""
    # -I (isolated: ignores PYTHON* variables, the working folder and user site-packages), -B (no byte-code files),
    # -X utf8 (the same text encoding on every system). Flags are used because -I ignores environment variables.
    command = [sys.executable, "-I", "-B", "-X", "utf8", "-c", guard, *args]
    try:
        done = subprocess.run(command, cwd=folder, env=child_environment(), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as expired:
        def text(value):
            return value.decode("utf-8", "replace") if isinstance(value, bytes) else (value or "")
        return Outcome(command, None, text(expired.stdout), text(expired.stderr), timed_out=True)
    return Outcome(command, done.returncode, done.stdout, done.stderr)


def run_unit_tests(folder: Path, timeout: float = TEST_SUITE_TIMEOUT) -> Outcome:
    """The generated project's own tests: python -m unittest discover -v, as its README says."""
    return run_python(folder, ["-m", "unittest", "discover", "-v"], timeout)


def imported_modules(source: str) -> set[str]:
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            modules.add(node.module)
    return modules


def missing_dependencies(workspace: Path) -> set[str]:
    """Imported top-level modules that are neither in the standard library nor part of the project itself."""
    local = {p.name for p in workspace.iterdir() if p.is_dir()} | {p.stem for p in workspace.glob("*.py")}
    wanted = set()
    for path in workspace.rglob("*.py"):
        wanted |= {name.split(".")[0] for name in imported_modules(path.read_text(encoding="utf-8"))}
    return {name for name in wanted if name not in sys.stdlib_module_names and name not in local}


def count_test_methods(workspace: Path) -> int:
    total = 0
    for path in (workspace / "tests").glob("test_*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                total += 1
    return total


def snapshot(folder: Path, skip=("__pycache__", ".git", ".aether", ".pytest_cache")) -> dict:
    """{relative path: (size, modified time)} for every file under the folder."""
    found = {}
    for current, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in files:
            path = Path(current) / name
            stat = path.stat()
            found[path.relative_to(folder).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return found


def generate(key: str, runs_dir: Path) -> tuple[ProjectContext, Path]:
    """Generate the template exactly as the web app does (plan, approve at Checkpoint A, Developer writes
    the files), with every file kept under `runs_dir`. Returns the finished run and its project folder."""
    store = RunStore(runs_dir)
    manager = ManagerAgent(planner=RuleBasedPlanner(), log=lambda message: None)
    agents = [PlannerAgent(RuleBasedPlanner()), DeveloperAgent(store)]
    context = manager.run_pipeline(REQUIREMENTS[key], agents, store=store)
    manager.approve(context, "A", True, store=store)
    manager.resume(context, agents, store)
    return context, store.workspace_dir(context.run_id)


def default_runs_listing() -> dict | None:
    """The user's real runs folder as it is now (None if it does not exist), to prove it is left alone."""
    folder = Path(os.environ.get("AETHER_RUNS_DIR") or Path.home() / "AETHER-projects")
    return snapshot(folder) if folder.is_dir() else None


class GeneratedProjects(unittest.TestCase):
    """Base class: generates all three templates once into one temporary folder, and records what the
    real AETHER folder and the user's runs folder looked like before, to prove they are untouched."""

    projects: dict
    contexts: dict
    temp_root: Path

    @classmethod
    def setUpClass(cls):
        cls.project_before = snapshot(ROOT)
        cls.runs_before = default_runs_listing()
        temp = tempfile.TemporaryDirectory(prefix="aether-smoke-")
        cls.addClassCleanup(temp.cleanup)
        cls.temp_root = Path(temp.name).resolve()
        cls.projects, cls.contexts = {}, {}
        for key in REQUIREMENTS:
            cls.contexts[key], cls.projects[key] = generate(key, cls.temp_root / "runs")

    def skip_if_unsupported(self, key: str) -> None:
        if NEEDS_SQLITE[key] and SQLITE_MISSING:
            self.skipTest(f"{SQLITE_MISSING}; the {key} template needs it, so it was NOT run.")
        missing = missing_dependencies(self.projects[key])
        if missing:
            self.skipTest(f"The {key} template needs modules that are not installed ({', '.join(sorted(missing))}); "
                          "it was NOT run. Nothing is installed automatically.")


class GenerationTests(GeneratedProjects):
    def test_all_three_templates_are_covered(self):
        self.assertEqual({t.key for t in TEMPLATES}, set(REQUIREMENTS))
        self.assertEqual(set(EXPECTED_FILES), set(REQUIREMENTS))

    def test_each_run_completes_and_the_developer_reports_success(self):
        for key, context in self.contexts.items():
            with self.subTest(template=key):
                self.assertEqual(context.status, COMPLETED)
                self.assertEqual(context.project_type, key)

    def test_exactly_the_expected_files_are_generated(self):
        for key, expected in EXPECTED_FILES.items():
            with self.subTest(template=key):
                self.assertEqual(set(Workspace(self.projects[key]).list_files()), expected)
                self.assertEqual({record.path for record in self.contexts[key].files}, expected)

    def test_template_definitions_agree_with_the_expected_files(self):
        for template in TEMPLATES:
            with self.subTest(template=template.key):
                self.assertEqual({f.path for f in template.files}, EXPECTED_FILES[template.key])

    def test_every_file_stays_inside_the_temporary_workspace(self):
        for key, project in self.projects.items():
            inside = project.resolve()
            self.assertTrue(inside.is_relative_to(self.temp_root), key)
            for path in project.rglob("*"):
                with self.subTest(template=key, file=path.name):
                    self.assertTrue(path.resolve().is_relative_to(inside))
                    self.assertFalse(path.is_symlink())
            for relative in EXPECTED_FILES[key]:
                self.assertTrue((project / relative).is_file(), f"{key}: {relative} is missing")

    def test_nothing_was_written_outside_the_workspace_folder_of_each_run(self):
        for key, context in self.contexts.items():
            run_folder = self.temp_root / "runs" / context.run_id
            with self.subTest(template=key):
                self.assertEqual({p.name for p in run_folder.iterdir()}, {"context.json", "events.jsonl", "workspace"})

    def test_files_on_disk_match_the_recorded_checksums(self):
        import hashlib
        for key, context in self.contexts.items():
            for record in context.files:
                data = (self.projects[key] / record.path).read_bytes()
                with self.subTest(template=key, file=record.path):
                    self.assertEqual(hashlib.sha256(data).hexdigest(), record.sha256)
                    self.assertEqual(len(data), record.size)
                    self.assertGreater(record.size, 0 if not record.path.endswith("__init__.py") else -1)

    def test_essential_project_files_are_present_and_not_empty(self):
        for key, project in self.projects.items():
            with self.subTest(template=key):
                self.assertTrue((project / ENTRY_POINT[key]).read_text(encoding="utf-8").strip())
                readme = (project / "README.md").read_text(encoding="utf-8")
                self.assertIn(REQUIREMENTS[key], readme)
                self.assertIn("python -m unittest discover", readme)
                self.assertTrue((project / "tests" / "__init__.py").is_file())
                self.assertTrue(list((project / "tests").glob("test_*.py")))
        for key in ("todo_web", "library_api"):
            package = "todo_app" if key == "todo_web" else "library_api"
            schema = (self.projects[key] / package / "schema.sql").read_text(encoding="utf-8")
            self.assertIn("CREATE TABLE", schema)

    def test_no_template_placeholders_are_left_in_any_file(self):
        for key, project in self.projects.items():
            for relative in EXPECTED_FILES[key]:
                with self.subTest(template=key, file=relative):
                    self.assertNotIn("{{", (project / relative).read_text(encoding="utf-8"))

    def test_no_dependency_files_and_only_standard_library_imports(self):
        for key, project in self.projects.items():
            with self.subTest(template=key):
                self.assertEqual({p.name for p in project.rglob("*")} & FORBIDDEN_FILES, set())
                self.assertEqual(missing_dependencies(project), set())

    def test_generated_code_imports_no_network_or_process_modules(self):
        for key, project in self.projects.items():
            for path in project.rglob("*.py"):
                bad = {m for m in imported_modules(path.read_text(encoding="utf-8"))
                       if any(m == n or m.startswith(n + ".") for n in NETWORK_MODULES)}
                with self.subTest(template=key, file=path.name):
                    self.assertEqual(bad, set())


class SyntaxTests(GeneratedProjects):
    def test_every_generated_python_file_compiles(self):
        checked = 0
        for key, project in self.projects.items():
            for path in sorted(project.rglob("*.py")):
                with self.subTest(template=key, file=path.relative_to(project).as_posix()):
                    compile(path.read_text(encoding="utf-8"), str(path), "exec")  # no .pyc is written
                    checked += 1
        self.assertGreaterEqual(checked, 3 + 9 + 7)  # a loose floor so "compiled nothing" cannot pass

    def test_a_syntax_error_would_be_caught(self):
        with tempfile.TemporaryDirectory() as temp:
            broken = Path(temp) / "broken.py"
            broken.write_text("def oops(:\n    pass\n", encoding="utf-8")
            with self.assertRaises(SyntaxError):
                compile(broken.read_text(encoding="utf-8"), str(broken), "exec")


class GeneratedTestSuiteTests(GeneratedProjects):
    """Run each project's own unit tests in a child process. A template only counts as passing if the child
    exited normally, unittest printed OK, and every test file of the project actually ran."""

    def check_suite(self, key: str) -> Outcome:
        self.skip_if_unsupported(key)
        project = self.projects[key]
        outcome = run_unit_tests(project)
        detail = f"\n--- stdout ---\n{outcome.stdout}\n--- stderr ---\n{outcome.stderr}"
        self.assertFalse(outcome.timed_out, f"{key}: the generated tests timed out{detail}")
        self.assertEqual(outcome.returncode, 0, f"{key}: the generated tests failed{detail}")
        self.assertTrue(outcome.tests_passed, f"{key}: unittest did not report OK{detail}")
        self.assertGreaterEqual(outcome.tests_run, count_test_methods(project), f"{key}: tests were not found{detail}")
        for test_file in (project / "tests").glob("test_*.py"):
            self.assertIn(f"tests.{test_file.stem}", outcome.stderr, f"{key}: {test_file.name} was not run")
        return outcome

    def test_calculator_own_tests_pass_in_a_subprocess(self):
        outcome = self.check_suite("calculator")
        self.assertEqual(outcome.command[0], sys.executable)

    def test_todo_web_own_tests_pass_in_a_subprocess(self):
        self.check_suite("todo_web")

    def test_library_api_own_tests_pass_in_a_subprocess(self):
        self.check_suite("library_api")

    def test_the_subprocess_uses_the_current_interpreter_in_isolated_mode(self):
        outcome = run_python(self.projects["calculator"], ["-c", "import sys; print(sys.executable); "
                                                                  "print(sys.flags.isolated, sys.flags.dont_write_bytecode)"],
                             SHORT_TIMEOUT)
        self.assertTrue(outcome.succeeded, outcome.stderr)
        executable, flags = outcome.stdout.splitlines()
        self.assertEqual(Path(executable).resolve(), Path(sys.executable).resolve())
        self.assertEqual(flags, "1 1")  # isolated mode, byte-code writing off

    def test_the_subprocess_runs_inside_the_project_with_a_stripped_environment(self):
        project = self.projects["calculator"]
        outcome = run_python(project, ["-c", "import os; print(os.getcwd()); print(sorted(os.environ))"], SHORT_TIMEOUT)
        self.assertEqual(Path(outcome.stdout.splitlines()[0]).resolve(), project.resolve())
        for name in ("PYTHONPATH", "AETHER_RUNS_DIR", "AETHER_PLANNER", "AETHER_HISTORY_FILE"):
            self.assertNotIn(name, outcome.stdout)

    def test_the_project_tests_leave_no_files_behind_except_in_the_temporary_project(self):
        self.skip_if_unsupported("todo_web")
        before = snapshot(self.projects["todo_web"], skip=())  # skip nothing: byte-code folders count too
        run_unit_tests(self.projects["todo_web"])
        after = snapshot(self.projects["todo_web"], skip=())
        self.assertEqual(after, before)  # in-memory databases, no .db files, no __pycache__
        self.assertFalse([name for name in after if "__pycache__" in name or name.endswith((".pyc", ".db"))])


class GeneratedCommandLineTests(GeneratedProjects):
    """The calculator is a plain command-line program, so it is run for real (arguments in, text out).
    The two servers are only IMPORTED, never started: starting them would open a network port."""

    def test_calculator_command_line_gives_the_right_answers(self):
        project = self.projects["calculator"]
        for args, expected in ((["12", "+", "30"], "42"), (["7", "/", "2"], "3.5"), (["6", "x", "7"], "42"),
                               (["0.1", "+", "0.2"], "0.3")):
            with self.subTest(args=args):
                outcome = run_python(project, ["calculator.py", *args], SHORT_TIMEOUT)
                self.assertTrue(outcome.succeeded, outcome.stderr)
                self.assertEqual(outcome.stdout.strip(), expected)

    def test_calculator_reports_errors_with_a_failing_exit_code(self):
        outcome = run_python(self.projects["calculator"], ["calculator.py", "1", "/", "0"], SHORT_TIMEOUT)
        self.assertEqual(outcome.returncode, 1)
        self.assertIn("Cannot divide by zero", outcome.stderr)
        self.assertFalse(outcome.succeeded)

    def test_calculator_help_works(self):
        outcome = run_python(self.projects["calculator"], ["calculator.py", "--help"], SHORT_TIMEOUT)
        self.assertTrue(outcome.succeeded)
        self.assertIn("Usage", outcome.stdout)

    def test_the_servers_import_without_starting_and_listen_on_this_computer_only(self):
        for key in ("todo_web", "library_api"):
            with self.subTest(template=key):
                if SQLITE_MISSING:
                    self.skipTest(f"{SQLITE_MISSING}; {key} was NOT imported.")
                snippet = ("import server; assert callable(server.main) and callable(server.make_handler); "
                           "print(server.HOST, server.PORT)")
                outcome = run_python(self.projects[key], ["-c", snippet], SHORT_TIMEOUT)
                self.assertTrue(outcome.succeeded, outcome.stderr)
                self.assertEqual(outcome.stdout.strip(), "127.0.0.1 8000")
                self.assertFalse(list(self.projects[key].glob("*.db")))  # importing created no database


class FailureDetectionTests(GeneratedProjects):
    """The smoke test must be able to FAIL. Each case damages a COPY of a generated project (never the
    original, never AETHER itself) and checks that the damage is noticed."""

    def damaged_copy(self, key: str, relative: str, old: str, new: str) -> Path:
        copy = self.temp_root / f"damaged-{key}-{len(list(self.temp_root.glob('damaged-*')))}"
        shutil.copytree(self.projects[key], copy)
        path = copy / relative
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        return copy

    def test_a_wrong_calculation_makes_the_generated_tests_fail(self):
        copy = self.damaged_copy("calculator", "calculator.py", '"+": lambda a, b: a + b', '"+": lambda a, b: a - b')
        outcome = run_unit_tests(copy)
        self.assertFalse(outcome.succeeded)
        self.assertFalse(outcome.tests_passed)
        self.assertRegex(outcome.stderr, r"FAILED \(failures=\d+")
        self.assertIn("FAIL:", outcome.stderr)

    def test_a_syntax_error_in_generated_code_is_reported_as_a_failure(self):
        copy = self.damaged_copy("calculator", "calculator.py", "def parse_number(", "def parse_number(:\n    pass\ndef x(")
        outcome = run_unit_tests(copy)
        self.assertFalse(outcome.tests_passed)
        self.assertNotEqual(outcome.returncode, 0)

    def test_a_broken_database_layer_is_noticed(self):
        if SQLITE_MISSING:
            self.skipTest(SQLITE_MISSING)
        copy = self.damaged_copy("library_api", "library_api/schema.sql", "CREATE TABLE IF NOT EXISTS books",
                                 "CREATE TABLE IF NOT EXISTS book_list")
        self.assertFalse(run_unit_tests(copy).tests_passed)

    def test_the_original_generated_project_was_not_changed_by_the_damaged_copies(self):
        self.assertIn("a + b", (self.projects["calculator"] / "calculator.py").read_text(encoding="utf-8"))

    def test_a_project_with_no_tests_is_not_reported_as_passing(self):
        empty = self.temp_root / "empty-project"
        (empty / "tests").mkdir(parents=True)
        (empty / "tests" / "__init__.py").write_text("", encoding="utf-8")
        outcome = run_unit_tests(empty)
        self.assertFalse(outcome.tests_passed)  # "NO TESTS RAN" is not a pass

    def test_a_missing_dependency_is_detected_so_the_test_can_skip_instead_of_claiming_success(self):
        project = self.temp_root / "needs-a-package"
        project.mkdir()
        (project / "app.py").write_text("import json\nimport some_package_nobody_installed\nimport local_helper\n",
                                        encoding="utf-8")
        (project / "local_helper.py").write_text("", encoding="utf-8")
        self.assertEqual(missing_dependencies(project), {"some_package_nobody_installed"})


class SubprocessHandlingTests(GeneratedProjects):
    def write(self, name: str, text: str) -> Path:
        folder = self.temp_root / f"scratch-{name}"
        folder.mkdir(exist_ok=True)
        (folder / f"{name}.py").write_text(text, encoding="utf-8")
        return folder

    def test_a_failing_process_is_captured_not_raised(self):
        folder = self.write("fails", "import sys\nprint('to stdout')\nprint('to stderr', file=sys.stderr)\nsys.exit(3)\n")
        outcome = run_python(folder, ["fails.py"], SHORT_TIMEOUT)
        self.assertEqual((outcome.returncode, outcome.stdout.strip(), outcome.stderr.strip()), (3, "to stdout", "to stderr"))
        self.assertFalse(outcome.succeeded)
        self.assertFalse(outcome.timed_out)

    def test_an_uncaught_exception_is_captured_with_its_traceback(self):
        folder = self.write("crash", "raise RuntimeError('boom')\n")
        outcome = run_python(folder, ["crash.py"], SHORT_TIMEOUT)
        self.assertNotEqual(outcome.returncode, 0)
        self.assertIn("RuntimeError: boom", outcome.stderr)

    def test_a_process_that_runs_too_long_is_stopped_and_reported(self):
        folder = self.write("slow", "import time\nprint('started', flush=True)\ntime.sleep(60)\n")
        outcome = run_python(folder, ["slow.py"], timeout=2)
        self.assertTrue(outcome.timed_out)
        self.assertFalse(outcome.succeeded)
        self.assertFalse(outcome.tests_passed)
        self.assertIsNone(outcome.returncode)

    def test_network_access_is_blocked_inside_the_subprocess(self):
        folder = self.write("net", "import socket\n"
                                   "for attempt in (lambda: socket.create_connection(('127.0.0.1', 9)),\n"
                                   "                lambda: socket.socket().bind(('127.0.0.1', 0)),\n"
                                   "                lambda: socket.getaddrinfo('localhost', 80)):\n"
                                   "    try:\n        attempt()\n        print('ALLOWED')\n"
                                   "    except OSError as error:\n        print('blocked:', error)\n")
        outcome = run_python(folder, ["net.py"], SHORT_TIMEOUT)
        self.assertTrue(outcome.succeeded, outcome.stderr)
        self.assertNotIn("ALLOWED", outcome.stdout)
        self.assertEqual(outcome.stdout.count("blocked:"), 3)

    def test_unittest_output_is_only_trusted_when_the_process_also_exited_cleanly(self):
        fake = Outcome(["x"], 1, "", "Ran 5 tests in 0.1s\n\nOK\n")
        self.assertEqual(fake.tests_run, 5)
        self.assertFalse(fake.tests_passed)  # printed OK but exit code 1
        self.assertFalse(Outcome(["x"], 0, "", "Ran 0 tests in 0.0s\n\nOK\n").tests_passed)
        self.assertFalse(Outcome(["x"], None, "", "Ran 5 tests in 0.1s\n\nOK\n", timed_out=True).tests_passed)
        self.assertTrue(Outcome(["x"], 0, "", "Ran 5 tests in 0.1s\n\nOK\n").tests_passed)

    def test_the_child_environment_has_no_python_or_aether_settings(self):
        with unittest_env({"PYTHONPATH": "C:/somewhere", "AETHER_RUNS_DIR": "C:/runs", "HTTP_PROXY": "x"}):
            env = child_environment()
        for name in ("PYTHONPATH", "AETHER_RUNS_DIR", "HTTP_PROXY"):
            self.assertNotIn(name, env)
        self.assertEqual([name for name in env if name.startswith(("PYTHON", "AETHER"))], [])


class unittest_env:
    """Temporarily set environment variables (restored on exit)."""

    def __init__(self, values):
        self.values, self.saved = values, {}

    def __enter__(self):
        for key, value in self.values.items():
            self.saved[key] = os.environ.get(key)
            os.environ[key] = value

    def __exit__(self, *exc):
        for key, old in self.saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


class IsolationAndCleanupTests(unittest.TestCase):
    """Run a whole generation + test cycle and prove it touched nothing it should not have."""

    def test_temporary_workspace_is_removed_and_nothing_else_is_touched(self):
        project_before, runs_before = snapshot(ROOT), default_runs_listing()
        with tempfile.TemporaryDirectory(prefix="aether-smoke-cleanup-") as temp:
            root = Path(temp)
            context, project = generate("calculator", root / "runs")
            self.assertTrue(project.is_dir() and project.resolve().is_relative_to(root.resolve()))
            outcome = run_unit_tests(project)
            self.assertTrue(outcome.tests_passed, outcome.stderr)
        self.assertFalse(root.exists())  # everything generated is gone
        self.assertEqual(snapshot(ROOT), project_before)       # AETHER's own files: no change at all
        self.assertEqual(default_runs_listing(), runs_before)  # the user's ~/AETHER-projects: no change

    def test_generation_with_a_temporary_store_never_uses_the_real_runs_folder(self):
        before = default_runs_listing()
        with tempfile.TemporaryDirectory(prefix="aether-smoke-store-") as temp:
            context, _ = generate("calculator", Path(temp) / "runs")
            self.assertEqual(RunStore(Path(temp) / "runs").list_runs(), [context.run_id])
        self.assertEqual(default_runs_listing(), before)

    def test_the_whole_class_based_run_left_the_aether_project_unchanged(self):
        """GeneratedProjects.setUpClass records the project before; compare after a full suite run."""
        class Probe(GeneratedProjects):
            def test_probe(self):
                pass

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Probe)
        result = unittest.TestResult()
        suite.run(result)
        self.assertTrue(result.wasSuccessful(), result.errors)
        self.assertEqual(snapshot(ROOT), Probe.project_before)
        self.assertEqual(default_runs_listing(), Probe.runs_before)
        self.assertFalse(Probe.temp_root.exists())  # the class cleanup removed the temporary folder


if __name__ == "__main__":
    unittest.main()
