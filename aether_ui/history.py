"""Local run history, stored as a JSON file on this computer.

Default location: .aether/history.json in the project folder.
Set the AETHER_HISTORY_FILE environment variable to use a different file (the tests do this).
"""

import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from aether.schema import KIND_HISTORY_ENTRY, SCHEMA_VERSION, VERSION_KEY, SchemaError, check_type, upgrade
from aether_ui.workflow import RunResult, result_from_dict, result_to_dict

MAX_ENTRIES = 25
DEFAULT_PATH = Path(__file__).resolve().parent.parent / ".aether" / "history.json"


def history_path() -> Path:
    return Path(os.environ.get("AETHER_HISTORY_FILE", DEFAULT_PATH))


def _read_raw(path: Path) -> tuple[list, bool]:
    """Everything in the file exactly as saved, and whether the file exists but is damaged."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], False
    except OSError:
        return [], True
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [], True
    return (data, False) if isinstance(data, list) else ([], True)


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


def save_run(result: RunResult, path: Path | None = None) -> dict:
    """Add a run to the top of the history, keeping at most MAX_ENTRIES.

    Existing entries are kept exactly as they were saved (even ones this version cannot read), and a damaged
    history file is copied aside before it is replaced, so saving never destroys data."""
    path = path or history_path()
    entry = {VERSION_KEY: SCHEMA_VERSION, "id": uuid.uuid4().hex, "result": result_to_dict(result)}
    existing, damaged = _read_raw(path)
    if damaged and path.exists():
        shutil.copy2(path, path.with_name(f"{path.name}.damaged-{datetime.now():%Y%m%d%H%M%S}"))
    _write(path, ([entry] + existing)[:MAX_ENTRIES])
    return entry


def entry_to_result(entry: dict) -> RunResult:
    """The RunResult in a history entry. Raises SchemaError if the entry cannot be read."""
    entry = upgrade(entry, KIND_HISTORY_ENTRY, "Saved run")
    check_type(entry.get("result"), (dict,), "Saved run", "result")
    return result_from_dict(entry["result"])


def clear_history(path: Path | None = None) -> None:
    _write(path or history_path(), [])


def _write(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    temp.replace(path)  # replace in one step so a crash can't leave half a file
