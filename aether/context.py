"""Shared project context: everything the agents know about one run, plus its activity log.

Agents never call each other. The Manager passes a ProjectContext to each agent in turn,
applies what the agent returns, and records an AgentEvent for everything that really happened.
Runs are saved on this computer in ~/AETHER-projects/<run_id>/ (set AETHER_RUNS_DIR to change it).
Generated projects are never written inside AETHER's own source folder.
"""

import json
import os
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path

from aether.fileio import write_text_atomic
from aether.models import Plan, TaskBrief
from aether.schema import (
    KIND_CONTEXT, SCHEMA_VERSION, VERSION_KEY, SchemaError, brief_from_dict, build, check_items, check_type,
    plan_from_dict, upgrade,
)

# Event statuses.
STARTED, DONE, FAILED, SKIPPED, WAITING = "started", "done", "failed", "skipped", "waiting"
# Run statuses.
RUNNING, COMPLETED, AWAITING_APPROVAL, CANCELLED = "running", "completed", "awaiting_approval", "cancelled"
# Approval decisions.
APPROVED, REJECTED = "approved", "rejected"

SOURCE_ROOT = Path(__file__).resolve().parent.parent  # AETHER's own code
DEFAULT_RUNS_DIR = Path.home() / "AETHER-projects"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class AgentEvent:
    """One thing an agent actually did, with the status it really reached."""

    time: str
    agent: str
    action: str
    status: str
    detail: str = ""
    duration_ms: float | None = None


@dataclass
class FileRecord:
    """A project file written through the Workspace."""

    path: str
    author: str
    sha256: str
    size: int
    covers: list[int] = field(default_factory=list)  # plan step numbers this file carries out


@dataclass
class Approval:
    """A human decision at a checkpoint (for example, approving the plan)."""

    checkpoint: str
    decision: str
    time: str
    note: str = ""
    choice: str = ""  # an option the user explicitly picked, e.g. a template override at Checkpoint A


@dataclass
class ProjectContext:
    run_id: str
    requirement: str
    created_at: str
    status: str = RUNNING
    brief: TaskBrief | None = None
    plan: Plan | None = None
    project_type: str | None = None  # the Developer's template key, e.g. "todo_web"
    files: list[FileRecord] = field(default_factory=list)
    approvals: list[Approval] = field(default_factory=list)
    events: list[AgentEvent] = field(default_factory=list)
    notes: dict[str, str] = field(default_factory=dict)  # e.g. why an AI agent fell back

    @classmethod
    def new(cls, requirement: str) -> "ProjectContext":
        return cls(run_id=uuid.uuid4().hex[:12], requirement=" ".join(requirement.split()), created_at=now())

    def record(self, agent: str, action: str, status: str, detail: str = "",
               duration_ms: float | None = None) -> AgentEvent:
        event = AgentEvent(now(), agent, action, status, detail, duration_ms)
        self.events.append(event)
        return event

    def __post_init__(self):
        # Set when loaded from saved data. Plain attributes, not dataclass fields, so they take no part in
        # equality, in asdict(), or in what agents may update.
        self.load_warnings: list[str] = []   # things the loader noticed but tolerated, e.g. unknown fields
        self.unknown_fields: dict = {}       # top-level fields this version does not know; saved back unchanged

    def to_dict(self) -> dict:
        """The saved form: the current schema version, then every field, then any unknown fields kept from loading."""
        data = {VERSION_KEY: SCHEMA_VERSION, **asdict(self)}
        for key, value in self.unknown_fields.items():
            data.setdefault(key, value)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectContext":
        """Load a saved run, whatever schema version it was saved with (see aether/schema.py).

        Raises SchemaError for a newer or invalid schema version, a missing or wrongly typed essential field
        (run_id, requirement, created_at) or a malformed part. Missing optional fields get defaults. Unknown fields are
        kept in `unknown_fields` and listed in `load_warnings`."""
        data = upgrade(data, KIND_CONTEXT, "Run record")
        where, warnings = "Run record", []
        missing = [name for name in ("run_id", "requirement", "created_at") if name not in data]
        if missing:
            raise SchemaError(f"{where} is missing required field(s): {', '.join(missing)}.")
        for name in ("run_id", "requirement", "created_at"):
            check_type(data[name], (str,), where, name)
        if not data["run_id"].strip():
            raise SchemaError(f"{where}: 'run_id' must not be empty.")

        status = data.get("status", RUNNING)
        check_type(status, (str,), where, "status")
        if status not in RUN_STATUSES:
            warnings.append(f"{where}: unrecognised status {status!r} was kept as it is.")
        project_type = data.get("project_type")
        check_type(project_type, (str, type(None)), where, "project_type")
        notes = data.get("notes") or {}
        check_type(notes, (dict,), where, "notes")
        for key, value in notes.items():
            check_type(key, (str,), f"{where}.notes", "key")
            check_type(value, (str,), f"{where}.notes", key)

        context = cls(
            run_id=data["run_id"],
            requirement=data["requirement"],
            created_at=data["created_at"],
            status=status,
            brief=None if data.get("brief") is None else brief_from_dict(data["brief"], "brief", warnings),
            plan=None if data.get("plan") is None else plan_from_dict(data["plan"], "plan", warnings),
            project_type=project_type,
            files=_load_list(data, "files", FileRecord, FILE_TYPES, warnings, covers=True),
            approvals=_load_list(data, "approvals", Approval, APPROVAL_TYPES, warnings),
            events=[event_from_dict(item, f"events[{index}]", warnings)
                    for index, item in enumerate(_list(data, "events"))],
            notes=dict(notes),
        )
        context.unknown_fields = {key: value for key, value in data.items()
                                  if key not in {f.name for f in fields(cls)} and key != VERSION_KEY}
        for key in context.unknown_fields:
            warnings.append(f"{where}: unknown field {key!r} was kept and will be saved back unchanged.")
        context.load_warnings = warnings
        return context


RUN_STATUSES = {RUNNING, COMPLETED, AWAITING_APPROVAL, CANCELLED, FAILED}
EVENT_TYPES = {"time": (str,), "agent": (str,), "action": (str,), "status": (str,), "detail": (str,),
               "duration_ms": (int, float, type(None))}
FILE_TYPES = {"path": (str,), "author": (str,), "sha256": (str,), "size": (int,), "covers": (list,)}
APPROVAL_TYPES = {"checkpoint": (str,), "decision": (str,), "time": (str,), "note": (str,), "choice": (str,)}


def event_from_dict(data, where: str, warnings: list[str]) -> AgentEvent:
    """One activity event (an item of context.json's `events`, or a line of events.jsonl), validated."""
    return build(AgentEvent, data, where, EVENT_TYPES, warnings)


def _list(data: dict, name: str) -> list:
    value = data.get(name)
    if value is None:
        return []
    check_type(value, (list,), "Run record", name)
    return value


def _load_list(data: dict, name: str, cls, types: dict, warnings: list[str], covers: bool = False) -> list:
    items = [build(cls, item, f"{name}[{index}]", types, warnings) for index, item in enumerate(_list(data, name))]
    if covers:
        for index, item in enumerate(items):
            check_items(item.covers, (int,), f"{name}[{index}]", "covers")
    return items


class RunStore:
    """Saves each run's context.json and events.jsonl in its own folder."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root or os.environ.get("AETHER_RUNS_DIR", DEFAULT_RUNS_DIR))
        if self.root.resolve().is_relative_to(SOURCE_ROOT):
            raise ValueError(f"Runs cannot be stored inside AETHER's own folder ({SOURCE_ROOT}). "
                             "Choose a folder outside it.")

    def run_dir(self, run_id: str) -> Path:
        if not run_id.isalnum():
            raise ValueError(f"Invalid run id {run_id!r}.")
        return self.root / run_id

    def workspace_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "workspace"

    def save(self, context: ProjectContext) -> Path:
        folder = self.run_dir(context.run_id)
        folder.mkdir(parents=True, exist_ok=True)
        _write_atomic(folder / "context.json", json.dumps(context.to_dict(), indent=2))
        _write_atomic(folder / "events.jsonl", "".join(json.dumps(asdict(e)) + "\n" for e in context.events))
        return folder

    def load(self, run_id: str) -> ProjectContext:
        """Load a saved run. A missing file raises OSError; a damaged or unsupported one raises SchemaError."""
        text = (self.run_dir(run_id) / "context.json").read_text(encoding="utf-8")
        try:
            context = ProjectContext.from_dict(json.loads(text))
        except json.JSONDecodeError as error:
            raise SchemaError(f"Run {run_id!r}: context.json is not valid JSON ({error}).") from error
        except SchemaError as error:
            raise SchemaError(f"Run {run_id!r}: {error}") from error
        if context.run_id != run_id:
            context.load_warnings.append(f"The folder is named {run_id!r} but the record says {context.run_id!r}.")
        return context

    def load_events(self, run_id: str) -> list[AgentEvent]:
        """The events saved in a run's events.jsonl, validated line by line (blank lines are skipped)."""
        text = (self.run_dir(run_id) / "events.jsonl").read_text(encoding="utf-8")
        events = []
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            where = f"Run {run_id!r}: events.jsonl line {number}"
            try:
                item = json.loads(line)
            except json.JSONDecodeError as error:
                raise SchemaError(f"{where} is not valid JSON ({error}).") from error
            events.append(event_from_dict(item, where, []))
        return events

    def list_runs(self) -> list[str]:
        """Saved run ids, newest first."""
        if not self.root.is_dir():
            return []
        runs = [p for p in self.root.iterdir() if (p / "context.json").is_file()]
        return [p.name for p in sorted(runs, key=lambda p: (p / "context.json").stat().st_mtime, reverse=True)]


def _write_atomic(path: Path, text: str) -> None:
    write_text_atomic(path, text)  # a unique temp file, then one replace: a crash can't leave half a file
