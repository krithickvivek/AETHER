"""Run the existing Manager -> Planner workflow and record what actually happened.

Every status shown in the web interface comes from a RunResult built here,
so the page never shows progress that did not really occur.
"""

import time
from dataclasses import asdict, dataclass, field
from datetime import datetime

from aether.manager import ManagerAgent
from aether.models import Plan, PlanStep, TaskBrief
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
    """Run ManagerAgent with the given planner (RuleBasedPlanner by default)."""
    tracer = TracingPlanner(planner or RuleBasedPlanner())
    log: list[str] = []
    manager = ManagerAgent(planner=tracer, log=log.append)

    started_at = datetime.now().isoformat(timespec="seconds")
    start = time.perf_counter()
    plan, error, error_details = None, None, None
    try:
        plan = manager.run(requirement)
    except ValueError as exc:
        # Expected input problems, e.g. an empty requirement.
        error = str(exc)
    except Exception as exc:
        error = "Something went wrong while generating the plan."
        error_details = f"{type(exc).__name__}: {exc}"
    duration_ms = (time.perf_counter() - start) * 1000
    fallback_reason = getattr(tracer.inner, "fallback_reason", None)

    return RunResult(
        requirement=" ".join(requirement.split()),
        started_at=started_at,
        duration_ms=duration_ms,
        stages=_build_stages(tracer, plan, fallback_reason),
        log=log,
        brief=tracer.received,
        plan=plan,
        error=error,
        error_details=error_details,
        fallback_reason=fallback_reason,
    )


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
    return asdict(result)


def result_from_dict(data: dict) -> RunResult:
    brief = TaskBrief(**data["brief"]) if data.get("brief") else None
    plan = None
    if data.get("plan"):
        plan_data = data["plan"]
        plan = Plan(
            requirement=plan_data["requirement"],
            planner_name=plan_data["planner_name"],
            steps=[PlanStep(**step) for step in plan_data["steps"]],
        )
    return RunResult(
        requirement=data["requirement"],
        started_at=data["started_at"],
        duration_ms=data["duration_ms"],
        stages=[Stage(**stage) for stage in data["stages"]],
        log=list(data["log"]),
        brief=brief,
        plan=plan,
        error=data.get("error"),
        error_details=data.get("error_details"),
        fallback_reason=data.get("fallback_reason"),  # absent in older history entries
    )
