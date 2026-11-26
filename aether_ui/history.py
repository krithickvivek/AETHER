"""Local run history, stored as a JSON file on this computer.

Default location: .aether/history.json in the project folder.
Set the AETHER_HISTORY_FILE environment variable to use a different file (the tests do this).

Safe to use from several threads and several programs (for example Streamlit and the web API) at once:
  * Every change (save_run, clear_history) runs under an exclusive lock on a sidecar file, history.json.lock, so two
    writers can never both start from the same old list and overwrite each other's new entry (a "lost update").
  * The file is replaced atomically (aether.fileio.write_text_atomic), so a reader sees the old or the new list,
    never half of one, and a failed write leaves the old file exactly as it was.
  * Before changing the file, save_run reads it strictly. If it cannot be READ (locked, permission, disk error)
    it raises HistoryReadError and writes nothing: a read failure is never mistaken for "damaged" or "empty".
    Only a file that was read and is really corrupt (not JSON, not a list, not UTF-8) is copied aside as
    history.json.damaged-<time> and then replaced.
  * Persistence failures are raised as HistoryError (an OSError, so existing `except OSError` code keeps working).
Reading (load_history, load_history_report) takes no lock and creates nothing. It keeps its old, lenient behaviour:
a damaged or unreadable file shows as an empty history (known issue K36); use the strict reader if that matters.
"""

import contextlib
import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from aether.fileio import LockTimeout, exclusive_lock, read_text_retry, write_text_atomic
from aether.schema import KIND_HISTORY_ENTRY, SCHEMA_VERSION, VERSION_KEY, SchemaError, check_type, upgrade
from aether_ui.workflow import RunResult, result_from_dict, result_to_dict

MAX_ENTRIES = 25
DEFAULT_PATH = Path(__file__).resolve().parent.parent / ".aether" / "history.json"


class HistoryError(OSError):
    """The history could not be read or saved. Nothing was changed. The message is safe to show (no paths)."""


class HistoryReadError(HistoryError):
    """The history file exists but could not be read, so it was left exactly as it was."""


class HistoryLockError(HistoryError):
    """Another operation (or program) kept the history busy for too long, so nothing was changed."""


class HistoryWriteError(HistoryError):
    """The history file could not be written, so the previous file was left in place."""


def history_path() -> Path:
    return Path(os.environ.get("AETHER_HISTORY_FILE", DEFAULT_PATH))


def lock_path(path: Path) -> Path:
    """The sidecar lock file for a history file (an empty file; it is only ever locked, never read)."""
    return path.with_name(path.name + ".lock")


# ------------------------------------------------------------------ reading

def _parse(text: str) -> tuple[list, bool]:
    """(entries, damaged) for the text of a history file."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [], True
    return (data, False) if isinstance(data, list) else ([], True)


def _read_raw(path: Path) -> tuple[list, bool]:
    """Everything in the file exactly as saved, and whether the file exists but is damaged or unreadable.
    Lenient on purpose (used for display): an unreadable file counts as damaged here. Saving never uses this."""
    try:
        text = read_text_retry(path)
    except FileNotFoundError:
        return [], False
    except UnicodeDecodeError:
        return [], True
    except OSError:
        return [], True
    return _parse(text)


def _read_for_update(path: Path) -> tuple[list, bool]:
    """Like _read_raw, but a file that cannot be READ raises HistoryReadError instead of looking damaged.
    A missing file is ([], False); a file that was read and is corrupt is ([], True)."""
    try:
        text = read_text_retry(path)
    except FileNotFoundError:
        return [], False
    except UnicodeDecodeError:
        return [], True
    except OSError as error:
        raise HistoryReadError("The history file could not be read, so it was not changed.") from error
    return _parse(text)


def load_history_report(path: Path | None = None) -> tuple[list[dict], list[str]]:
    """The usable saved runs (newest first, each upgraded to the current schema) and a problem
    description for every entry that was skipped. Skipped entries are not deleted from the file."""
    entries, problems = [], []
    for number, item in enumerate(_read_raw(path or history_path())[0], start=1):
        where = f"Saved run #{number}"
        try:
            entry = upgrade(item, KIND_HISTORY_ENTRY, where)
            check_type(entry.get("id"), (str,), where, "id")
            check_type(entry.get("result"), (dict,), where, "result")
            result_from_dict(entry["result"])  # validate the whole record now, not when it is opened
        except SchemaError as error:
            problems.append(str(error))
            continue
        entries.append(entry)
    return entries, problems


def load_history(path: Path | None = None) -> list[dict]:
    """Return saved runs, newest first. A missing or damaged file gives an empty list, and entries that cannot
    be read (damaged, or saved by a newer AETHER) are left out; use load_history_report to see why."""
    return load_history_report(path)[0]


# ------------------------------------------------------------------ changing the history

@contextlib.contextmanager
def _locked(path: Path):
    """Exclusive access to the history file for a read-modify-write, with explicit errors."""
    with contextlib.ExitStack() as stack:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            stack.enter_context(exclusive_lock(lock_path(path)))
        except LockTimeout as error:
            raise HistoryLockError("The history is busy; it was not changed. Try again in a moment.") from error
        except OSError as error:
            raise HistoryWriteError("The history folder could not be prepared for saving.") from error
        _remove_stale_temp_files(path)  # we hold the lock, so any leftover temp file is from a crashed writer
        yield


@contextlib.contextmanager
def _as_history_errors():
    """Turn a plain OSError from saving into HistoryWriteError (HistoryErrors pass through unchanged)."""
    try:
        yield
    except HistoryError:
        raise
    except OSError as error:
        raise HistoryWriteError("The history could not be saved; the previous history was kept.") from error


def _remove_stale_temp_files(path: Path) -> None:
    for stale in path.parent.glob(f"{path.name}.*.tmp"):
        with contextlib.suppress(OSError):
            stale.unlink()


def _copy_aside(path: Path) -> Path:
    """Keep a copy of a corrupt history file before it is replaced. Never overwrites an earlier copy."""
    stamp = f"{datetime.now():%Y%m%d%H%M%S}"
    target = path.with_name(f"{path.name}.damaged-{stamp}")
    number = 1
    while target.exists():
        number += 1
        target = path.with_name(f"{path.name}.damaged-{stamp}-{number}")
    shutil.copy2(path, target)
    return target


def save_run(result: RunResult, path: Path | None = None) -> dict:
    """Add a run to the top of the history, keeping at most MAX_ENTRIES.

    Existing entries are kept exactly as they were saved (even ones this version cannot read). Safe against
    concurrent writers (see the module docstring). Raises HistoryError (an OSError) if the history cannot be read,
    locked or written; in every such case the existing file is left exactly as it was."""
    path = path or history_path()
    entry = {VERSION_KEY: SCHEMA_VERSION, "id": uuid.uuid4().hex, "result": result_to_dict(result)}
    with _as_history_errors(), _locked(path):
        existing, damaged = _read_for_update(path)
        if damaged:
            _copy_aside(path)  # a read, corrupt file: keep a copy, then start a new history
        _write(path, ([entry] + existing)[:MAX_ENTRIES])
    return entry


def entry_to_result(entry: dict) -> RunResult:
    """The RunResult in a history entry. Raises SchemaError if the entry cannot be read."""
    entry = upgrade(entry, KIND_HISTORY_ENTRY, "Saved run")
    check_type(entry.get("result"), (dict,), "Saved run", "result")
    return result_from_dict(entry["result"])


def clear_history(path: Path | None = None) -> None:
    """Delete every saved run (the owner's explicit request). Atomic and locked like save_run."""
    path = path or history_path()
    with _as_history_errors(), _locked(path):
        _write(path, [])


def _write(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps(entries, indent=2))
