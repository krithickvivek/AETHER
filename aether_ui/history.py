"""Local run history, stored as a JSON file on this computer.

Default location: .aether/history.json in the project folder.
Set the AETHER_HISTORY_FILE environment variable to use a different file (the tests do this).
"""

import json
import os
import uuid
from pathlib import Path

from aether_ui.workflow import RunResult, result_from_dict, result_to_dict

MAX_ENTRIES = 25
DEFAULT_PATH = Path(__file__).resolve().parent.parent / ".aether" / "history.json"


def history_path() -> Path:
    return Path(os.environ.get("AETHER_HISTORY_FILE", DEFAULT_PATH))


def load_history(path: Path | None = None) -> list[dict]:
    """Return saved runs, newest first. A missing or damaged file gives an empty list."""
    path = path or history_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def save_run(result: RunResult, path: Path | None = None) -> dict:
    """Add a run to the top of the history, keeping at most MAX_ENTRIES."""
    path = path or history_path()
    entry = {"id": uuid.uuid4().hex, "result": result_to_dict(result)}
    entries = [entry] + load_history(path)
    _write(path, entries[:MAX_ENTRIES])
    return entry


def entry_to_result(entry: dict) -> RunResult:
    return result_from_dict(entry["result"])


def clear_history(path: Path | None = None) -> None:
    _write(path or history_path(), [])


def _write(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    temp.replace(path)  # replace in one step so a crash can't leave half a file
