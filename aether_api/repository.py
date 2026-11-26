"""Read access to runs, history and generated files, through the existing AETHER core.

`RunRepository` is the interface the routers use, so a SQLite implementation can replace the file one later
(owner decision D4). `FileRunRepository` reads through RunStore, aether_ui.history and Workspace. It never writes.

Safety rules kept here:
  * A run is loaded and validated BEFORE a Workspace is constructed, because Workspace.__init__ creates its
    folder. A Workspace is only constructed for a run that has recorded files AND whose workspace folder already
    exists, so reading can never create a folder. (RunStore.run_dir only builds a path.)
  * A failed read is never turned into a write: history is only read, and a history file that cannot be read is
    reported as damaged or unreadable instead of looking like an empty history.
  * A brief PermissionError (another process replacing a file at that moment, common on Windows) is retried.
"""

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, TypeVar

from aether.context import FileRecord, ProjectContext, RunStore
from aether.schema import SchemaError
from aether.workspace import Workspace, WorkspaceError
from aether_ui import history
from aether_ui.workflow import RunResult

T = TypeVar("T")
READ_ATTEMPTS = 3
READ_RETRY_DELAY = 0.05  # seconds


# ------------------------------------------------------------------ domain errors (no HTTP here)

class RepositoryError(Exception):
    """Base class. `message` is safe to show: it never contains an absolute path."""

    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message


class RunNotFound(RepositoryError):
    pass


class RunUnreadable(RepositoryError):
    """The saved run is damaged, invalid, or from a newer AETHER."""


class RunsFolderInvalid(RepositoryError):
    pass


class StorageUnavailable(RepositoryError):
    """The disk could not be read (for example a file that stayed locked)."""


class HistoryEntryNotFound(RepositoryError):
    pass


class HistoryUnavailable(RepositoryError):
    def __init__(self, state: str, message: str = ""):
        super().__init__(message)
        self.state = state


class FileNotRecorded(RepositoryError):
    pass


class InvalidFilePath(RepositoryError):
    pass


class FileNotText(RepositoryError):
    pass


# ------------------------------------------------------------------ results

@dataclass
class HistoryRead:
    file_state: str                     # ok | missing | damaged | unreadable
    entries: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


@dataclass
class FolderState:
    valid: bool
    root: Path | None = None


@dataclass
class FileContent:
    record: FileRecord
    text: str | None
    missing: bool
    current_sha256: str | None


def retry_read(action: Callable[[], T]) -> T:
    """Run a read; if it hits a PermissionError, wait briefly and try again (a few times)."""
    for attempt in range(READ_ATTEMPTS):
        try:
            return action()
        except PermissionError:
            if attempt == READ_ATTEMPTS - 1:
                raise
            time.sleep(READ_RETRY_DELAY)
    raise AssertionError("unreachable")


class RunRepository(Protocol):
    def runs_folder(self) -> FolderState: ...
    def load_run(self, run_id: str) -> ProjectContext: ...
    def context_state(self, run_id: str | None) -> tuple[str, ProjectContext | None]: ...
    def read_history(self) -> HistoryRead: ...
    def history_entry(self, entry_id: str) -> tuple[dict, RunResult]: ...
    def snapshot_for_run(self, run_id: str) -> RunResult | None: ...
    def workspace_exists(self, run_id: str) -> bool: ...
    def read_file(self, context: ProjectContext, path: str) -> FileContent: ...


class FileRunRepository:
    """The file-based implementation: ~/AETHER-projects/<run id>/ and .aether/history.json."""

    # -------------------------------------------------------------- runs

    def runs_folder(self) -> FolderState:
        try:
            return FolderState(True, RunStore().root)
        except ValueError:  # inside AETHER's own folder
            return FolderState(False)

    def _store(self) -> RunStore:
        try:
            return RunStore()
        except ValueError as error:
            raise RunsFolderInvalid("Generated projects cannot be stored inside AETHER's own folder. "
                                    "Set AETHER_RUNS_DIR to a folder outside it.") from error

    def load_run(self, run_id: str) -> ProjectContext:
        store = self._store()
        try:
            return retry_read(lambda: store.load(run_id))
        except SchemaError as error:
            raise RunUnreadable(str(error)) from error
        except UnicodeDecodeError as error:
            raise RunUnreadable(f"Run {run_id!r}: context.json is not valid text.") from error
        except ValueError as error:  # an id that RunStore.run_dir refuses (not letters and digits)
            raise RunNotFound("There is no saved run with this id.") from error
        except (FileNotFoundError, NotADirectoryError) as error:
            raise RunNotFound("There is no saved run with this id.") from error
        except OSError as error:
            raise StorageUnavailable("The saved run could not be read from disk.") from error

    def context_state(self, run_id: str | None) -> tuple[str, ProjectContext | None]:
        """("ok", context), ("missing", None) or ("unreadable", None). Never raises."""
        if not run_id:
            return "missing", None
        try:
            return "ok", self.load_run(run_id)
        except RunNotFound:
            return "missing", None
        except RepositoryError:
            return "unreadable", None

    def workspace_exists(self, run_id: str) -> bool:
        try:
            return self._store().workspace_dir(run_id).is_dir()  # a stat only: nothing is created
        except (RepositoryError, ValueError, OSError):
            return False

    # -------------------------------------------------------------- history

    def read_history(self) -> HistoryRead:
        path = history.history_path()
        state, raw_count = self._probe_history(path)
        if state in ("damaged", "unreadable"):
            return HistoryRead(state)
        entries, problems = history.load_history_report(path)  # the core's own validation, entry by entry
        if len(entries) + len(problems) != raw_count:  # the file changed or could not be read a second time
            return HistoryRead("unreadable")
        return HistoryRead(state, entries, problems)

    @staticmethod
    def _probe_history(path: Path) -> tuple[str, int]:
        """(file state, number of raw entries). load_history_report treats an unreadable or damaged file as an
        empty history, so the file is checked here first to tell the owner the truth."""
        try:
            text = retry_read(lambda: path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return "missing", 0
        except UnicodeDecodeError:
            return "damaged", 0
        except OSError:
            return "unreadable", 0
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return "damaged", 0
        if not isinstance(data, list):
            return "damaged", 0
        return "ok", len(data)

    def history_entry(self, entry_id: str) -> tuple[dict, RunResult]:
        read = self.read_history()
        if read.file_state in ("damaged", "unreadable"):
            raise HistoryUnavailable(read.file_state, "The history file could not be read.")
        for entry in read.entries:
            if entry.get("id") == entry_id:
                try:
                    return entry, history.entry_to_result(entry)
                except SchemaError as error:
                    raise HistoryUnavailable("damaged", str(error)) from error
        raise HistoryEntryNotFound("There is no saved history entry with this id.")

    def snapshot_for_run(self, run_id: str) -> RunResult | None:
        """The history snapshot of a run (it holds the run time and the Manager log), if there is one. Best effort."""
        try:
            for entry in self.read_history().entries:
                result = history.entry_to_result(entry)
                if result.run_id == run_id:
                    return result
        except (SchemaError, OSError, ValueError):
            pass
        return None

    # -------------------------------------------------------------- generated files

    def read_file(self, context: ProjectContext, path: str) -> FileContent:
        """One generated file's text. The run is already loaded; a Workspace is built only if its folder exists."""
        wanted = path.replace("\\", "/")
        record = next((r for r in context.files if r.path == wanted), None)
        workspace_dir = self._store().workspace_dir(context.run_id)
        try:
            has_folder = workspace_dir.is_dir()  # a stat only: nothing is created
        except OSError as error:
            raise StorageUnavailable("The project folder could not be read from disk.") from error
        if not context.files or not has_folder:
            if record is None:
                raise FileNotRecorded("This run has no generated file with that path.")
            return FileContent(record, None, missing=True, current_sha256=None)

        workspace = Workspace(workspace_dir)  # the folder exists, so this creates nothing
        try:
            workspace.resolve(path)  # traversal, absolute paths, links out of the folder, disallowed file types
        except WorkspaceError as error:  # the core's message echoes the requested path, so use a fixed one
            raise InvalidFilePath("A project file path must be relative, stay inside the project folder (no '..'), "
                                  "and have an allowed file type.") from error
        if record is None:
            raise FileNotRecorded("This run has no generated file with that path.")
        try:
            is_file = workspace.resolve(record.path).is_file()  # removed, or replaced by a folder (a stat only)
        except OSError as error:
            raise StorageUnavailable("The file could not be read from disk.") from error
        if not is_file:
            return FileContent(record, None, missing=True, current_sha256=None)
        try:
            text = retry_read(lambda: workspace.read_text(record.path))
        except UnicodeDecodeError as error:
            raise FileNotText("The file is not valid UTF-8 text.") from error
        except PermissionError as error:
            raise StorageUnavailable("The file could not be read from disk.") from error
        except OSError:  # removed, or replaced by a folder
            return FileContent(record, None, missing=True, current_sha256=None)
        return FileContent(record, text, missing=False,
                           current_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
