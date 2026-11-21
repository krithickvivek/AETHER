"""Tests for the History page and the saved-run opening workflow, run without a browser.

What is covered: the History list, "Open" (restoring the saved result and its project context),
"Delete all", the About page, and what happens when saved data is missing, damaged or from a newer
AETHER. Everything uses temporary history and run folders and a blocked network. Generated code is
never run. The real history file and run folder are never written; setUpClass/tearDownClass compare
their metadata (existence, size, modified time; never their contents) before and after the class.

STREAMLIT INTERNALS USED BY THIS FILE (read this before upgrading Streamlit)
    app.py declares its pages with st.Page(function, url_path=...). AppTest.switch_page() only accepts
    script files, so it cannot reach them (it raises "Unable to find script at history"). Two helpers
    therefore use Streamlit internals, and these three helpers are the ONLY code in this file that does:
      open_page()  - imports `calc_hash` from streamlit.testing.v1.app_test (an internal module) and sets
                     AppTest's PRIVATE attribute `_page_hash` to calc_hash(url_path), which is how
                     Streamlit 1.57 identifies a function page.
      show_workspace() - sets the same private `_page_hash` back to "" (the default page). After "Open",
                     the app switches to the Workspace page, but AppTest still remembers the History
                     page, so the NEXT run() would show History again; this keeps later steps (for example
                     choosing a file) on the Workspace page.
      html_text()  - reads `element.proto.body` (the internal protobuf) of st.html elements, because
                     AppTest has no public accessor for st.html. The History page's list rows and empty
                     state are rendered with st.html.
    Everything else uses AppTest's public API (from_file, run, button, selectbox, caption, ...) and
    st.cache_data.clear().
    UPGRADE RISK: a Streamlit upgrade may move or rename `calc_hash`, `_page_hash` or `proto.body`. The
    helpers then FAIL LOUDLY (AssertionError / AttributeError); they never skip. The module only skips
    when Streamlit itself is not installed. This is proved by MissingCalcHashTests, including a test that
    runs the History tests in a child process with `calc_hash` removed and requires failures, not skips.
    If one of them breaks: fix open_page() or html_text() (or use a public AppTest page API if one exists
    by then); do not edit the other tests.

Two behaviours are pinned as they are today (known issues K35 and K36, not defects fixed here):
    O1  A saved run whose folder/context cannot be loaded still opens from its saved result, with no
        message that the files and approval state are gone.
    O2  A damaged history file looks exactly like an empty history, with no explanation.
If either is improved later, the tests marked O1 / O2 should be updated on purpose.
"""

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

try:
    import streamlit as st
    from streamlit.testing.v1 import AppTest
except ImportError:  # Streamlit is optional for the command-line app: skip only when it is NOT installed
    st = None
    AppTest = None

from aether.context import CANCELLED, COMPLETED, AWAITING_APPROVAL, DEFAULT_RUNS_DIR, RunStore
from aether.schema import SCHEMA_VERSION, VERSION_KEY
from aether_ui import history
from aether_ui.workflow import decide_plan, start_project

TESTS_DIR = str(Path(__file__).resolve().parent)
APP_FILE = str(Path(TESTS_DIR).parent / "app.py")
TIMEOUT = 60
REQUIREMENT = "A REST API with a database"  # matches no template, so its checkpoint cannot be approved
SECOND_REQUIREMENT = "A calculator that adds and subtracts"  # fits the calculator template


def no_network(*args, **kwargs):
    raise OSError("Network access is blocked in tests")


# ------------------------------------------------------------------ the helpers that use Streamlit internals

def open_page(name: str) -> "AppTest":
    """Run app.py showing the page whose url_path is `name` ("history" or "about").

    INTERNALS: imports calc_hash from streamlit.testing.v1.app_test and sets AppTest._page_hash (private).
    If either has changed it raises AssertionError saying so; it never skips. See the module docstring."""
    try:
        from streamlit.testing.v1.app_test import calc_hash
    except ImportError as error:
        raise AssertionError(
            "Streamlit no longer provides streamlit.testing.v1.app_test.calc_hash, so the History and About "
            "pages cannot be opened under AppTest. Update open_page() in tests/test_history_page.py.") from error
    app = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT)
    if not hasattr(app, "_page_hash"):
        raise AssertionError(
            "AppTest no longer has the private `_page_hash` attribute, so the History and About pages "
            "cannot be opened under AppTest. Update open_page() in tests/test_history_page.py.")
    app._page_hash = calc_hash(name)
    return app.run()


def show_workspace(app) -> None:
    """Make later run() calls show the default (Workspace) page again, e.g. after "Open" switched to it.

    INTERNALS: sets AppTest._page_hash (private) back to "", its initial value meaning the default page."""
    if not hasattr(app, "_page_hash"):
        raise AssertionError("AppTest no longer has the private `_page_hash` attribute. "
                             "Update show_workspace() in tests/test_history_page.py.")
    app._page_hash = ""


def html_text(app) -> str:
    """The text of every st.html element on the page.

    INTERNALS: reads `element.proto.body` (Streamlit's internal protobuf); AppTest has no public st.html
    accessor. If the attribute disappears this raises AttributeError (a loud failure, never a skip)."""
    return " ".join(str(element.proto.body) for element in app.get("html"))


# ------------------------------------------------------------------ ordinary helpers

def snapshot(root: Path) -> dict[str, bytes]:
    """Every file under root with its full contents, to prove a read-only action changed nothing."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def real_user_state() -> dict:
    """Metadata only (existence, size, modified time) of the REAL history file and the REAL run folder.

    Contents are never read. Used to prove the tests did not touch real user data."""
    def meta(path: Path):
        try:
            info = path.stat()
        except OSError:
            return None
        return (info.st_size, info.st_mtime_ns)

    runs = {}
    if DEFAULT_RUNS_DIR.is_dir():
        runs = {p.relative_to(DEFAULT_RUNS_DIR).as_posix(): meta(p) for p in DEFAULT_RUNS_DIR.rglob("*")}
    return {"history": meta(history.DEFAULT_PATH), "runs": runs}


def open_buttons(app) -> list:
    return [b for b in app.button if (b.key or "").startswith("open-")]


def all_text(elements) -> str:
    return " ".join(e.value for e in elements)


@unittest.skipIf(AppTest is None, "Streamlit is not installed")
class HistoryPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.real_state_before = real_user_state()

    @classmethod
    def tearDownClass(cls):
        after = real_user_state()
        if after != cls.real_state_before:
            raise AssertionError("The History page tests changed the REAL history file or the REAL run folder "
                                 f"({history.DEFAULT_PATH} / {DEFAULT_RUNS_DIR}).")

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        base = Path(self.temp_dir.name)
        self.history_file = base / "history.json"
        self.runs = base / "runs"
        patch_env = mock.patch.dict(os.environ, {"AETHER_HISTORY_FILE": str(self.history_file),
                                                 "AETHER_RUNS_DIR": str(self.runs)})
        patch_env.start()
        os.environ.pop("AETHER_PLANNER", None)
        patch_net = mock.patch("urllib.request.urlopen", side_effect=no_network)
        patch_net.start()
        self.addCleanup(patch_env.stop)
        self.addCleanup(patch_net.stop)
        # app.py caches the Ollama status for 15 s per process (@st.cache_data); clear it so no test sees a
        # value cached by another test module (the Workspace page is rendered after "Open").
        st.cache_data.clear()
        self.store = RunStore(self.runs)

    # ------------------------------------------------------------------ helpers

    def seed(self, requirement: str = REQUIREMENT, *, with_store: bool = True):
        """Save a real run the way the app does (Manager pipeline, paused at Checkpoint A) and add it to the
        history. Returns (result, context)."""
        result, context = start_project(requirement, store=self.store if with_store else None)
        history.save_run(result, self.history_file)
        return result, context

    def context_file(self, run_id: str) -> Path:
        return self.store.run_dir(run_id) / "context.json"

    def open_first_run(self):
        """Open the History page and click Open on the first (newest) saved run."""
        app = open_page("history")
        self.assertFalse(app.exception)
        buttons = open_buttons(app)
        self.assertTrue(buttons, "the History page shows no Open button")
        buttons[0].click().run()
        self.assertFalse(app.exception, "opening a saved run raised an exception")
        return app

    # ------------------------------------------------------------------ reaching the pages

    def test_helper_reaches_the_history_page(self):
        """Guards the page-selection workaround: if it stops reaching the History page this fails clearly."""
        self.seed()
        app = open_page("history")
        self.assertFalse(app.exception)
        self.assertTrue(
            open_buttons(app) and "Delete all" in [b.label for b in app.button],
            "open_page('history') did not show the History page. Streamlit's AppTest internals "
            "(_page_hash / calc_hash) have probably changed; update open_page() in this file.")
        self.assertNotIn("generate", [b.key for b in app.button], "the Workspace page was shown instead")

    def test_about_page_renders(self):
        app = open_page("about")
        self.assertFalse(app.exception)
        self.assertIn("About this prototype", html_text(app))  # the About page's own title
        self.assertIn("AETHER", all_text(app.markdown))
        self.assertNotIn("generate", [b.key for b in app.button], "the Workspace page was shown instead")
        self.assertEqual(len(app.text_area), 0, "the Workspace requirement box should not be on the About page")
        self.assertEqual(open_buttons(app), [])

    def test_tests_use_temporary_data_only(self):
        temp = Path(self.temp_dir.name).resolve()
        history_path, runs_root = history.history_path().resolve(), RunStore().root.resolve()
        self.assertTrue(history_path.is_relative_to(temp))
        self.assertTrue(runs_root.is_relative_to(temp))
        self.assertNotEqual(history_path, history.DEFAULT_PATH.resolve())
        self.assertNotEqual(runs_root, DEFAULT_RUNS_DIR.resolve())
        # A page run really uses those temporary paths: the seeded run is listed from the temporary file.
        self.seed()
        self.assertEqual(len(open_buttons(open_page("history"))), 1)

    def test_real_state_check_detects_changes(self):
        """The before/after check used by tearDownClass can actually fail (run against temporary stand-ins)."""
        with tempfile.TemporaryDirectory() as stand_in:
            fake_history, fake_runs = Path(stand_in) / "h.json", Path(stand_in) / "runs"
            fake_runs.mkdir()
            with mock.patch.object(history, "DEFAULT_PATH", fake_history), \
                    mock.patch(f"{__name__}.DEFAULT_RUNS_DIR", fake_runs):
                before = real_user_state()
                self.assertEqual(before, real_user_state())
                fake_history.write_text("[]", encoding="utf-8")
                self.assertNotEqual(before, real_user_state())
                before = real_user_state()
                (fake_runs / "new-run").mkdir()
                self.assertNotEqual(before, real_user_state())

    # ------------------------------------------------------------------ the list

    def test_empty_history_shows_the_empty_state(self):
        app = open_page("history")
        self.assertFalse(app.exception)
        self.assertEqual(open_buttons(app), [])
        self.assertNotIn("Delete all", [b.label for b in app.button])
        self.assertEqual(len(app.caption), 0)
        self.assertIn("No saved runs yet", html_text(app))

    def test_one_saved_run_is_listed_with_a_singular_caption(self):
        self.seed()
        app = open_page("history")
        self.assertFalse(app.exception)
        self.assertEqual(len(open_buttons(app)), 1)
        self.assertIn("1 saved run (newest first", app.caption[0].value)
        self.assertNotIn("1 saved runs", app.caption[0].value)
        self.assertIn(REQUIREMENT, html_text(app))

    def test_saved_runs_are_listed_newest_first(self):
        self.seed(REQUIREMENT)
        self.seed(SECOND_REQUIREMENT)
        app = open_page("history")
        self.assertFalse(app.exception)
        self.assertIn("2 saved runs (newest first", app.caption[0].value)
        self.assertEqual(len(open_buttons(app)), 2)
        html = html_text(app)
        self.assertLess(html.index(SECOND_REQUIREMENT), html.index(REQUIREMENT))
        open_buttons(app)[0].click().run()
        self.assertEqual(app.session_state["requirement"], SECOND_REQUIREMENT)

    # ------------------------------------------------------------------ opening a saved run

    def test_open_restores_the_result_the_context_and_the_requirement(self):
        result, context = self.seed()
        app = self.open_first_run()

        self.assertEqual(app.session_state["result_source"], "history")
        self.assertEqual(app.session_state["requirement"], REQUIREMENT)
        restored = app.session_state["result"]
        self.assertEqual(restored.run_id, context.run_id)
        self.assertEqual(restored.plan, result.plan)
        self.assertEqual(restored.brief, result.brief)
        self.assertIsNotNone(app.session_state["context"])

    def test_open_switches_to_the_workspace_and_says_nothing_was_re_executed(self):
        result, _ = self.seed()
        app = self.open_first_run()

        self.assertIn("generate", [b.key for b in app.button], "the Workspace page should be showing")
        expected_time = datetime.fromisoformat(result.started_at).strftime("%d %b %Y, %H:%M")
        banner = all_text(app.info)
        self.assertIn(f"Showing a saved run from {expected_time}", banner)
        self.assertIn("nothing was re-executed", banner)
        self.assertEqual(all_text(app.success), "", "a saved run must not claim a fresh plan was generated")

    def test_restored_context_matches_the_saved_run(self):
        _, context = self.seed()
        app = self.open_first_run()

        saved = self.store.load(context.run_id)
        restored = app.session_state["context"]
        self.assertEqual(restored.run_id, saved.run_id)
        self.assertEqual(restored.status, saved.status)
        self.assertEqual(restored.requirement, saved.requirement)
        self.assertEqual(restored.plan, saved.plan)
        self.assertEqual([dataclasses.asdict(e) for e in restored.events],
                         [dataclasses.asdict(e) for e in saved.events])
        self.assertEqual(restored.load_warnings, [])

    def test_open_a_run_awaiting_approval_shows_the_checkpoint(self):
        _, context = self.seed(SECOND_REQUIREMENT)
        self.assertEqual(self.store.load(context.run_id).status, AWAITING_APPROVAL)
        app = self.open_first_run()

        self.assertEqual(app.session_state["context"].status, AWAITING_APPROVAL)
        keys = [b.key for b in app.button]
        self.assertIn("approve_plan", keys)  # pinned (O1): a reopened pending run offers approval again
        self.assertIn("reject_plan", keys)
        self.assertFalse(app.button(key="approve_plan").disabled)  # a template fits, so approval is allowed

    def test_open_an_approved_run_shows_its_generated_files(self):
        _, context = self.seed(SECOND_REQUIREMENT)
        decide_plan(context, approved=True, store=self.store)
        saved = self.store.load(context.run_id)
        self.assertEqual(saved.status, COMPLETED)
        saved_paths = sorted(f.path for f in saved.files)
        self.assertIn("calculator.py", saved_paths)

        app = self.open_first_run()
        self.assertEqual(app.session_state["context"].status, COMPLETED)
        self.assertEqual(sorted(f.path for f in app.session_state["context"].files), saved_paths)
        keys = [b.key for b in app.button]
        self.assertNotIn("approve_plan", keys)
        self.assertNotIn("reject_plan", keys)

        # The file selector lists exactly the generated files (labels are "<path>  ·  <size> bytes").
        selector = app.selectbox(key="file_choice")
        self.assertEqual(len(selector.options), len(saved_paths))
        for path, label in zip(saved_paths, selector.options):
            self.assertTrue(label.startswith(path), f"{label!r} does not start with {path!r}")

        # Select a specific file (not whichever one the viewer opens on) and compare it with the file on disk.
        show_workspace(app)  # the next run() must stay on the Workspace page, not fall back to History
        selector.select("calculator.py").run()
        self.assertFalse(app.exception)
        on_disk = (self.store.workspace_dir(context.run_id) / "calculator.py").read_text(encoding="utf-8")
        self.assertIn(on_disk.strip(), [block.value.strip() for block in app.code])  # the viewer drops the final newline

    def test_open_a_rejected_run_says_no_files_were_generated(self):
        _, context = self.seed(SECOND_REQUIREMENT)
        decide_plan(context, approved=False, store=self.store, note="Rejected in a test")
        app = self.open_first_run()

        self.assertEqual(app.session_state["context"].status, CANCELLED)
        self.assertIn("You rejected this plan", all_text(app.info))
        self.assertNotIn("approve_plan", [b.key for b in app.button])

    def test_opening_a_run_changes_nothing_on_disk(self):
        self.seed()
        base = Path(self.temp_dir.name)
        before = snapshot(base)  # the history file and every run file, byte for byte
        self.assertTrue(before)
        self.open_first_run()
        self.assertEqual(snapshot(base), before)

    # ------------------------------------------------------------------ missing or invalid saved data (O1)

    def assert_opens_from_the_saved_result_only(self, app, run_id):
        """O1: no exception, the saved result is shown with the banner, but there is no context and
        no message about the missing data."""
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["result_source"], "history")
        self.assertEqual(app.session_state["result"].run_id, run_id)
        self.assertIsNone(app.session_state["context"])
        self.assertIn("Showing a saved run", all_text(app.info))
        self.assertEqual(len(app.error), 0)  # O1: nothing tells the user the context could not be loaded
        keys = [b.key for b in app.button]
        self.assertNotIn("approve_plan", keys)
        self.assertNotIn("reject_plan", keys)

    def test_missing_run_folder_still_opens_the_saved_result(self):
        _, context = self.seed()
        shutil.rmtree(self.store.run_dir(context.run_id))
        app = self.open_first_run()
        self.assert_opens_from_the_saved_result_only(app, context.run_id)
        self.assertFalse(self.store.run_dir(context.run_id).exists(), "opening must not recreate the folder")

    def test_missing_context_file_still_opens_the_saved_result(self):
        _, context = self.seed()
        self.context_file(context.run_id).unlink()
        app = self.open_first_run()
        self.assert_opens_from_the_saved_result_only(app, context.run_id)
        self.assertFalse(self.context_file(context.run_id).exists(), "opening must not recreate the file")

    def test_corrupt_context_file_still_opens_and_is_left_untouched(self):
        _, context = self.seed()
        path = self.context_file(context.run_id)
        path.write_text("{this is not json", encoding="utf-8")
        app = self.open_first_run()
        self.assert_opens_from_the_saved_result_only(app, context.run_id)
        self.assertEqual(path.read_text(encoding="utf-8"), "{this is not json")

    def test_context_from_a_newer_aether_still_opens_and_is_left_untouched(self):
        _, context = self.seed()
        path = self.context_file(context.run_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        data[VERSION_KEY] = SCHEMA_VERSION + 1
        path.write_text(json.dumps(data), encoding="utf-8")
        before = path.read_bytes()
        app = self.open_first_run()
        self.assert_opens_from_the_saved_result_only(app, context.run_id)
        self.assertEqual(path.read_bytes(), before)

    def test_context_with_the_wrong_shape_still_opens_and_is_left_untouched(self):
        _, context = self.seed()
        path = self.context_file(context.run_id)
        path.write_text("[1, 2, 3]", encoding="utf-8")
        app = self.open_first_run()
        self.assert_opens_from_the_saved_result_only(app, context.run_id)
        self.assertEqual(path.read_text(encoding="utf-8"), "[1, 2, 3]")

    def test_entry_saved_without_a_run_folder_still_opens(self):
        """A plan generated when generated projects could not be saved has no run id (load_context gets None)."""
        result, _ = self.seed(with_store=False)
        self.assertIsNone(result.run_id)
        app = self.open_first_run()
        self.assertIsNone(app.session_state["context"])
        self.assertIn("Showing a saved run", all_text(app.info))
        self.assertEqual(app.session_state["requirement"], REQUIREMENT)

    # ------------------------------------------------------------------ damaged history (O2)

    def test_damaged_history_file_looks_like_an_empty_history_and_is_kept(self):
        """O2: the page cannot tell a damaged file from an empty one, and it must not delete the file."""
        self.history_file.write_text("garbage, not json", encoding="utf-8")
        app = open_page("history")
        self.assertFalse(app.exception)
        self.assertEqual(open_buttons(app), [])
        self.assertIn("No saved runs yet", html_text(app))
        self.assertEqual(self.history_file.read_text(encoding="utf-8"), "garbage, not json")

    def test_unreadable_entries_are_skipped_for_the_right_reasons_and_kept_unchanged(self):
        self.seed()
        valid = json.loads(self.history_file.read_text(encoding="utf-8"))[0]
        newer = json.loads(json.dumps(valid))
        newer[VERSION_KEY] = SCHEMA_VERSION + 1
        newer["id"] = "from-a-newer-aether"
        broken = {"id": "broken", "result": "not a dict", VERSION_KEY: SCHEMA_VERSION}
        self.history_file.write_text(json.dumps([broken, newer, valid], indent=2), encoding="utf-8")
        before = self.history_file.read_bytes()

        # The reasons (the page itself does not show them, see O2): entry #1 is malformed, #2 is too new.
        usable, problems = history.load_history_report(self.history_file)
        self.assertEqual([entry["id"] for entry in usable], [valid["id"]])
        self.assertEqual(len(problems), 2)
        self.assertIn("Saved run #1", problems[0])
        self.assertIn("'result' must be an object", problems[0])
        self.assertIn("Saved run #2", problems[1])
        self.assertIn(f"schema version {SCHEMA_VERSION + 1}", problems[1])
        self.assertIn("newer AETHER", problems[1])

        # The page lists exactly the one usable entry, and Open works on it.
        app = open_page("history")
        self.assertFalse(app.exception)
        buttons = open_buttons(app)
        self.assertEqual([b.key for b in buttons], [f"open-{valid['id']}"])
        self.assertIn("1 saved run (newest first", app.caption[0].value)
        buttons[0].click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["requirement"], REQUIREMENT)

        self.assertEqual(self.history_file.read_bytes(), before, "skipped entries must stay in the file unchanged")

    # ------------------------------------------------------------------ delete all

    def test_delete_all_empties_the_history_and_keeps_the_run_folders(self):
        _, context = self.seed()
        self.seed(SECOND_REQUIREMENT)
        app = open_page("history")
        self.assertEqual(len(open_buttons(app)), 2)
        runs_before = snapshot(self.runs)
        self.assertTrue(runs_before)

        app.button(key="confirm_clear").click().run()

        self.assertFalse(app.exception)
        self.assertEqual(open_buttons(app), [])
        self.assertEqual(json.loads(self.history_file.read_text(encoding="utf-8")), [])
        self.assertEqual(history.load_history(self.history_file), [])
        self.assertEqual(snapshot(self.runs), runs_before)  # only the list is cleared, not the generated projects
        self.assertTrue(self.context_file(context.run_id).exists())


# A child process that removes calc_hash and runs the History tests, to prove the result is FAILURES, not skips.
_CHILD = r"""
import json, sys, unittest
sys.path.insert(0, sys.argv[1])
import streamlit.testing.v1.app_test as app_test
del app_test.calc_hash                      # simulate a Streamlit upgrade that moved or removed calc_hash
import test_history_page as module
suite = unittest.defaultTestLoader.loadTestsFromTestCase(module.HistoryPageTests)
result = unittest.TextTestRunner(stream=open(sys.argv[2], "w", encoding="utf-8"), verbosity=0).run(suite)
print(json.dumps({"run": result.testsRun, "skipped": len(result.skipped), "failed": len(result.failures),
                  "errors": len(result.errors), "ok": result.wasSuccessful(),
                  "guard_failed": any("test_helper_reaches_the_history_page" in str(t[0])
                                      for t in result.failures + result.errors)}))
"""


@unittest.skipIf(AppTest is None, "Streamlit is not installed")
class MissingCalcHashTests(unittest.TestCase):
    """Regression tests: a missing calc_hash (or _page_hash) must FAIL the History tests, never skip them."""

    def test_open_page_raises_an_explicit_error_when_calc_hash_is_missing(self):
        stub = types.ModuleType("streamlit.testing.v1.app_test")  # a module without calc_hash
        with mock.patch.dict(sys.modules, {"streamlit.testing.v1.app_test": stub}):
            with self.assertRaises(AssertionError) as caught:
                open_page("history")
        self.assertIn("calc_hash", str(caught.exception))

    def test_open_page_raises_an_explicit_error_when_page_hash_is_missing(self):
        with mock.patch.object(AppTest, "from_file", return_value=types.SimpleNamespace()):
            with self.assertRaises(AssertionError) as caught:
                open_page("history")
        self.assertIn("_page_hash", str(caught.exception))

    def test_history_suite_fails_not_skips_when_calc_hash_is_missing(self):
        """Runs HistoryPageTests in a child process with calc_hash removed. The old defect (an ImportError
        turning all tests into skips and the suite into 'OK (skipped=22)') must not come back."""
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "child_report.txt"
            done = subprocess.run([sys.executable, "-X", "utf8", "-c", _CHILD, TESTS_DIR, str(report)],
                                  capture_output=True, text=True, timeout=300, cwd=str(Path(TESTS_DIR).parent))
            detail = f"exit {done.returncode}\nstdout: {done.stdout[-1500:]}\nstderr: {done.stderr[-1500:]}"
            self.assertEqual(done.returncode, 0, detail)
            outcome = json.loads(done.stdout.strip().splitlines()[-1])
        expected = unittest.defaultTestLoader.loadTestsFromTestCase(HistoryPageTests).countTestCases()
        self.assertEqual(outcome["run"], expected)
        self.assertEqual(outcome["skipped"], 0, f"tests were skipped instead of failing: {outcome}")
        self.assertFalse(outcome["ok"], f"the suite passed although calc_hash is missing: {outcome}")
        self.assertGreater(outcome["failed"] + outcome["errors"], 0)
        self.assertTrue(outcome["guard_failed"], "the compatibility guard test did not fail")


if __name__ == "__main__":
    unittest.main()
