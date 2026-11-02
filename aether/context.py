"""Shared project context: everything the agents know about one run, plus its activity log.

Agents never call each other. The Manager passes a ProjectContext to each agent in turn,
applies what the agent returns, and records an AgentEvent for everything that really happened.
Runs are saved on this computer under .aether/runs/<run_id>/ (set AETHER_RUNS_DIR to change it).
"""

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from aether.models import Plan, PlanStep, TaskBrief

# Event statuses.
STARTED, DONE, FAILED, SKIPPED = "started", "done", "failed", "skipped"
# Run statuses.
RUNNING, COMPLETED = "running", "completed"

DEFAULT_RUNS_DIR = Path(__file__).resolve().parent.parent / ".aether" / "runs"


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


@dataclass
class Approval:
    """A human decision at a checkpoint (for example, approving the plan)."""

    checkpoint: str
    decision: str
    time: str
    note: str = ""


@dataclass
class ProjectContext:
    run_id: str
    requirement: str
    created_at: str
    status: str = RUNNING
    brief: TaskBrief | None = None
    plan: Plan | None = None
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

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectContext":
        plan = None
        if data.get("plan"):
            plan_data = data["plan"]
            plan = Plan(plan_data["requirement"], plan_data["planner_name"],
                        [PlanStep(**step) for step in plan_data["steps"]])
        return cls(
            run_id=data["run_id"],
            requirement=data["requirement"],
            created_at=data["created_at"],
            status=data.get("status", RUNNING),
            brief=TaskBrief(**data["brief"]) if data.get("brief") else None,
            plan=plan,
            files=[FileRecord(**f) for f in data.get("files", [])],
            approvals=[Approval(**a) for a in data.get("approvals", [])],
            events=[AgentEvent(**e) for e in data.get("events", [])],
            notes=dict(data.get("notes", {})),
        )


class RunStore:
    """Saves each run's context.json and events.jsonl in its own folder."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root or os.environ.get("AETHER_RUNS_DIR", DEFAULT_RUNS_DIR))

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
        data = json.loads((self.run_dir(run_id) / "context.json").read_text(encoding="utf-8"))
        return ProjectContext.from_dict(data)

    def list_runs(self) -> list[str]:
        """Saved run ids, newest first."""
        if not self.root.is_dir():
            return []
        runs = [p for p in self.root.iterdir() if (p / "context.json").is_file()]
        return [p.name for p in sorted(runs, key=lambda p: (p / "context.json").stat().st_mtime, reverse=True)]


def _write_atomic(path: Path, text: str) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)  # replace in one step so a crash can't leave half a file
