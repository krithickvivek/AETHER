"""Run the existing Manager -> Planner workflow and record what actually happened.

Every status shown in the web interface comes from a RunResult built here,
so the page never shows progress that did not really occur.
"""

import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime

from aether.agents import PlannerAgent
from aether.context import APPROVED, AWAITING_APPROVAL, CANCELLED
from aether.context import FAILED as EVENT_FAILED
from aether.context import ProjectContext, RunStore
from aether.developer import DeveloperAgent
from aether.manager import ManagerAgent
from aether.models import Plan, PlanStep, TaskBrief
from aether.schema import (
    KIND_RUN_RESULT, SCHEMA_VERSION, VERSION_KEY, SchemaError, brief_from_dict, build, check_items, check_type,
    plan_from_dict, upgrade,
)
from aether.planner import BASE_STEPS, FEATURE_STEPS, Planner, RuleBasedPlanner

DONE, FAILED, SKIPPED = "done", "failed", "skipped"

FEATURE_LABELS = {
    "authentication": "Authentication",
    "database": "Database / storage",
    "web_ui": "Web interface",
    "api": "API",
}


@dataclass
class Stage:
    """One step of the workflow, with the status it really reached."""

    agent: str
    title: str
    status: str
    detail: str = ""


@dataclass
class RunResult:
    requirement: str
    started_at: str
    duration_ms: float
    stages: list[Stage]
    log: list[str]
    brief: TaskBrief | None = None
    plan: Plan | None = None
    error: str | None = None
    error_details: str | None = None
    fallback_reason: str | None = None  # set when the AI planner failed and RuleBasedPlanner was used
    run_id: str | None = None  # the saved ProjectContext for this run (generated files, events), if any

    @property
    def ok(self) -> bool:
        return self.plan is not None

    @property
    def ai_generated(self) -> bool:
        return self.plan is not None and self.plan.planner_name != RuleBasedPlanner.name


class TracingPlanner(Planner):
    """Wraps a real planner and records the handoff without changing its behaviour."""

    def __init__(self, inner: Planner):
        self.inner = inner
        self.name = inner.name
        self.received: TaskBrief | None = None
        self.duration_ms = 0.0

    def create_plan(self, brief: TaskBrief) -> Plan:
        self.received = brief
        start = time.perf_counter()
        plan = self.inner.create_plan(brief)
        self.duration_ms = (time.perf_counter() - start) * 1000
        return plan


def run_workflow(requirement: str, planner: Planner | None = None) -> RunResult:
    """Run ManagerAgent with the given planner (RuleBasedPlanner by default), planning only."""
    return start_project(requirement, planner)[0]


def start_project(requirement: str, planner: Planner | None = None,
                  store: RunStore | None = None) -> tuple[RunResult, ProjectContext]:
    """Run the Manager pipeline. With a store, the Developer is included and the run pauses at
    Checkpoint A (plan approval) before any files are generated."""
    tracer = TracingPlanner(planner or RuleBasedPlanner())
    log: list[str] = []
    manager = ManagerAgent(planner=tracer, log=log.append)
    agents = [PlannerAgent(tracer)] + ([DeveloperAgent(store)] if store else [])

    started_at = datetime.now().isoformat(timespec="seconds")
    start = time.perf_counter()
    context = manager.run_pipeline(requirement, agents, store=store)
    duration_ms = (time.perf_counter() - start) * 1000

    error, error_details = None, None
    failed = next((e for e in context.events if e.status == EVENT_FAILED and e.agent in ("Manager", "Planner")
                   and e.action != "Build plan with local AI model"), None)
    if failed and failed.agent == "Manager":
        error = failed.detail  # expected input problems, e.g. an empty requirement
    elif failed:
        error = "Something went wrong while generating the plan."
        error_details = failed.detail
    fallback_reason = getattr(tracer.inner, "fallback_reason", None)

    result = RunResult(
        requirement=" ".join(requirement.split()),
        started_at=started_at,
        duration_ms=duration_ms,
        stages=_build_stages(tracer, context.plan, fallback_reason),
        log=log,
        brief=tracer.received,
        plan=context.plan,
        error=error,
        error_details=error_details,
        fallback_reason=fallback_reason,
        run_id=context.run_id if store else None,
    )
    return result, context


def project_agents(store: RunStore) -> list:
    """The agents of a full run, in order. The Planner has already run by the time a run is resumed."""
    return [PlannerAgent(RuleBasedPlanner()), DeveloperAgent(store)]


def decide_plan(context: ProjectContext, approved: bool, store: RunStore, note: str = "",
                override: str = "") -> ProjectContext:
    """Checkpoint A: record the user's decision, then generate files (approved) or cancel (rejected).

    `override` is a template key the user explicitly chose when no template fitted automatically."""
    manager = ManagerAgent(planner=RuleBasedPlanner(), log=lambda message: None)
    manager.approve(context, "A", approved, note, store, choice=override)
    return manager.resume(context, project_agents(store), store)


CURRENT, PENDING = "current", "pending"  # progress statuses, alongside DONE and FAILED


@dataclass
class ProgressStep:
    title: str
    status: str   # DONE, CURRENT, FAILED or PENDING
    detail: str = ""


def progress_steps(result: RunResult, context: ProjectContext | None) -> list[ProgressStep]:
    """Requirement -> Plan -> Approval -> Generated files, from what really happened in the run.

    Without a saved context (planning-only runs, or history saved before runs were stored),
    Approval and Generated files are left pending.
    """
    if result.brief is None:
        requirement = ProgressStep("Requirement", FAILED, "Not accepted")
    else:
        count = len(result.brief.features)
        requirement = ProgressStep("Requirement", DONE, f"{count} feature{'s' if count != 1 else ''}")

    if result.ok:
        plan = ProgressStep("Plan", DONE, f"{len(result.plan.steps)} steps")
    elif result.brief is not None:
        plan = ProgressStep("Plan", FAILED, "Planner failed")
    else:
        plan = ProgressStep("Plan", PENDING)

    approval, files = ProgressStep("Approval", PENDING), ProgressStep("Generated files", PENDING)
    if result.ok and context is not None:
        approved = any(a.checkpoint == "A" and a.decision == APPROVED for a in context.approvals)
        developer_failed = any(e.agent == "Developer" and e.status == EVENT_FAILED for e in context.events)
        if context.status == AWAITING_APPROVAL:
            approval = ProgressStep("Approval", CURRENT, "Waiting for you")
        elif context.status == CANCELLED:
            approval = ProgressStep("Approval", FAILED, "Rejected")
        elif approved:
            approval = ProgressStep("Approval", DONE, "Approved")
        if context.files:
            files = ProgressStep("Generated files", DONE, f"{len(context.files)} files")
        elif developer_failed:
            files = ProgressStep("Generated files", FAILED, "Not generated")
    return [requirement, plan, approval, files]


def format_duration(ms: float) -> str:
    return f"{ms:.1f} ms" if ms < 1000 else f"{ms / 1000:.1f} s"


def _build_stages(tracer: TracingPlanner, plan: Plan | None, fallback_reason: str | None = None) -> list[Stage]:
    brief = tracer.received
    handed_off = brief is not None

    if handed_off:
        count = len(brief.features)
        understand = Stage("Manager", "Understand requirement", DONE,
                           f"{count} feature{'s' if count != 1 else ''} detected")
        handoff = Stage("Manager", f"Hand off to {tracer.name}", DONE, "TaskBrief delivered")
    else:
        understand = Stage("Manager", "Understand requirement", FAILED, "Requirement was rejected")
        handoff = Stage("Manager", f"Hand off to {tracer.name}", SKIPPED, "Not reached")

    stages = [understand, handoff]
    if fallback_reason:
        # The AI planner was tried first and its result was not used.
        stages.append(Stage(tracer.name, "Build plan with local AI model", FAILED, fallback_reason))

    if plan is not None:
        phases = len(group_by_phase(plan.steps))
        title = "Build development plan (fallback)" if fallback_reason else "Build development plan"
        stages.append(Stage(plan.planner_name, title, DONE,
                            f"{len(plan.steps)} steps in {phases} phases, {format_duration(tracer.duration_ms)}"))
    elif handed_off:
        stages.append(Stage(tracer.name, "Build development plan", FAILED, "Planner raised an error"))
    else:
        stages.append(Stage(tracer.name, "Build development plan", SKIPPED, "Not reached"))

    return stages


def group_by_phase(steps: list[PlanStep]) -> dict[str, list[PlanStep]]:
    """Group steps by phase, keeping the order the planner returned them in."""
    groups: dict[str, list[PlanStep]] = {}
    for step in steps:
        groups.setdefault(step.phase, []).append(step)
    return groups


def step_origin(step: PlanStep, planner_name: str) -> str | None:
    """Which RuleBasedPlanner rule added this step: 'core', a feature key, or None if unknown."""
    if planner_name != RuleBasedPlanner.name:
        return None
    key = (step.phase, step.description)
    if key in BASE_STEPS:
        return "core"
    for feature, steps in FEATURE_STEPS.items():
        if key in steps:
            return feature
    return None


def plan_to_markdown(plan: Plan) -> str:
    """Format the plan as a Markdown document for downloading."""
    lines = ["# Development plan", "", f"**Requirement:** {plan.requirement}", ""]
    number = 1
    for phase, steps in group_by_phase(plan.steps).items():
        lines += [f"## {phase}", ""]
        for step in steps:
            lines.append(f"{number}. {step.description}")
            number += 1
        lines.append("")
    if plan.planner_name == RuleBasedPlanner.name:
        lines.append(f"_Generated by AETHER ({plan.planner_name}, rule-based)._")
    else:
        lines.append(f"_Generated by AETHER ({plan.planner_name}, a small local AI model). "
                     "AI-generated plans can be incomplete or wrong; review before use._")
    return "\n".join(lines)


def result_to_dict(result: RunResult) -> dict:
    """The saved form of a RunResult: the current schema version, then every field."""
    return {VERSION_KEY: SCHEMA_VERSION, **asdict(result)}


STAGE_TYPES = {"agent": (str,), "title": (str,), "status": (str,), "detail": (str,)}
RESULT_TYPES = {"requirement": (str,), "started_at": (str,), "duration_ms": (int, float), "stages": (list,),
                "log": (list,), "error": (str, type(None)), "error_details": (str, type(None)),
                "fallback_reason": (str, type(None)), "run_id": (str, type(None))}


def result_from_dict(data: dict, warnings: list[str] | None = None) -> RunResult:
    """Load a saved RunResult, whatever schema version it was saved with (see aether/schema.py).

    Fields added after the first release (fallback_reason, run_id) are optional. Raises SchemaError when an
    essential field is missing or malformed, or the version is newer than this AETHER understands. Unknown
    fields are ignored and, if a list is passed as `warnings`, reported there."""
    warnings = [] if warnings is None else warnings
    where = "Saved run"
    data = upgrade(data, KIND_RUN_RESULT, where)
    missing = [name for name in ("requirement", "started_at", "duration_ms", "stages", "log") if name not in data]
    if missing:
        raise SchemaError(f"{where} is missing required field(s): {', '.join(missing)}.")
    for name, expected in RESULT_TYPES.items():
        if name in data:
            check_type(data[name], expected, where, name)
    check_items(data["log"], (str,), where, "log")
    known = {f.name for f in fields(RunResult)}
    for name in sorted(set(data) - known - {VERSION_KEY}):
        warnings.append(f"{where}: ignored unknown field {name!r}.")
    return RunResult(
        requirement=data["requirement"],
        started_at=data["started_at"],
        duration_ms=data["duration_ms"],
        stages=[build(Stage, stage, f"{where}.stages[{index}]", STAGE_TYPES, warnings)
                for index, stage in enumerate(data["stages"])],
        log=list(data["log"]),
        brief=None if data.get("brief") is None else brief_from_dict(data["brief"], f"{where}.brief", warnings),
        plan=None if data.get("plan") is None else plan_from_dict(data["plan"], f"{where}.plan", warnings),
        error=data.get("error"),
        error_details=data.get("error_details"),
        fallback_reason=data.get("fallback_reason"),  # absent in older history entries
        run_id=data.get("run_id"),
    )
