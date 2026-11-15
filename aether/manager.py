"""Manager agent: understands a requirement and coordinates the workflow."""

import re
import time
from dataclasses import fields
from typing import TYPE_CHECKING, Callable

from aether.context import (
    APPROVED, AWAITING_APPROVAL, CANCELLED, COMPLETED, DONE, FAILED, REJECTED, RUNNING, SKIPPED, WAITING,
    Approval, ProjectContext, RunStore, now,
)
from aether.models import Plan, TaskBrief
from aether.planner import Planner

if TYPE_CHECKING:
    from aether.agents import Agent

# ProjectContext fields an agent may set through AgentResult.updates.
_UPDATABLE = {f.name for f in fields(ProjectContext)} - {
    "run_id", "requirement", "created_at", "status", "events", "approvals"}

# Keywords that tell the Manager a requirement needs a particular feature.
FEATURE_KEYWORDS = {
    "authentication": [
        "login", "log in", "logs in", "sign in", "sign up", "signup",
        "register", "password", "account", "accounts", "authentication",
    ],
    "database": [
        "database", "db", "save", "saves", "saved", "store", "stores", "stored",
        "persist", "record", "records",
    ],
    "web_ui": [
        "website", "web", "webpage", "web page", "frontend", "browser", "dashboard",
    ],
    "api": ["api", "apis", "endpoint", "endpoints"],
}


def detect_features(text: str) -> list[str]:
    """Return the features whose keywords appear in the text (as whole words)."""
    lowered = text.lower()
    found = []
    for feature, keywords in FEATURE_KEYWORDS.items():
        if any(re.search(rf"\b{re.escape(kw)}\b", lowered) for kw in keywords):
            found.append(feature)
    return found


class ManagerAgent:
    """Receives a requirement, builds a brief, and hands planning off to a Planner."""

    def __init__(self, planner: Planner, log: Callable[[str], None] = print):
        self.planner = planner
        self.log = log

    def understand(self, requirement: str) -> TaskBrief:
        """Clean up the requirement and work out which features it needs."""
        cleaned = " ".join(requirement.split())
        if not cleaned:
            raise ValueError("The requirement is empty. Please describe the software you want.")
        return TaskBrief(requirement=cleaned, features=detect_features(cleaned))

    def run(self, requirement: str) -> Plan:
        """Full workflow: understand -> hand off to the Planner -> return the plan."""
        brief = self.understand(requirement)
        self.log(f"[Manager] Requirement received: {brief.requirement}")
        self.log(f"[Manager] Detected features: {', '.join(brief.features) or 'none (core steps only)'}")

        self.log(f"[Manager] Handing off to {self.planner.name}...")
        plan = self.planner.create_plan(brief)
        self.log(f"[Manager] Received a plan with {len(plan.steps)} steps from {plan.planner_name}.")
        return plan

    def run_pipeline(self, requirement: str, agents: list["Agent"], store: RunStore | None = None) -> ProjectContext:
        """Run a requirement through a sequence of agents, sharing one ProjectContext.

        Each agent's result is recorded as it really happened. If an agent fails or crashes,
        the later agents are recorded as skipped. Before an agent that has a checkpoint, the run
        pauses (status AWAITING_APPROVAL) until approve() and resume() are called.
        With a store, the context is saved after every step.
        """
        context = ProjectContext.new(requirement)
        try:
            context.brief = self.understand(requirement)
        except ValueError as exc:
            context.record("Manager", "Understand requirement", FAILED, str(exc))
            return self._finish(context, agents, store, failed=True)

        count = len(context.brief.features)
        context.record("Manager", "Understand requirement", DONE, f"{count} feature{'s' if count != 1 else ''} detected")
        self.log(f"[Manager] Requirement received: {context.brief.requirement}")
        self.log(f"[Manager] Detected features: {', '.join(context.brief.features) or 'none (core steps only)'}")
        self._save(context, store)
        return self._run_agents(context, agents, store)

    def approve(self, context: ProjectContext, checkpoint: str, approved: bool, note: str = "",
                store: RunStore | None = None, choice: str = "") -> ProjectContext:
        """Record the user's decision at a checkpoint. Rejecting cancels the run.

        `choice` records an option the user explicitly picked with the approval (for example a
        template override), so the agent after the checkpoint acts on a recorded human decision.
        """
        if context.status != AWAITING_APPROVAL:
            raise ValueError("This run is not waiting for approval.")
        decision = APPROVED if approved else REJECTED
        choice = choice if approved else ""
        context.approvals.append(Approval(checkpoint, decision, now(), note, choice))
        if approved:
            detail = f"Approved with an explicit override: {choice}" if choice else "Approved"
        else:
            detail = f"Rejected{': ' + note if note else ''}"
        context.record("User", f"Checkpoint {checkpoint}", DONE if approved else FAILED, detail)
        self.log(f"[Manager] Checkpoint {checkpoint}: {decision} by the user.")
        if not approved:
            context.status = CANCELLED
        self._save(context, store)
        return context

    def resume(self, context: ProjectContext, agents: list["Agent"], store: RunStore | None = None) -> ProjectContext:
        """Continue a paused run with the same agent list. Agents that already finished are not re-run."""
        if context.status == CANCELLED:
            done = self._done_agents(context)
            for agent in agents:
                if (agent.name, agent.action) not in done and not self._skipped(context, agent):
                    context.record(agent.name, agent.action, SKIPPED, "Not run: the plan was rejected")
            self._save(context, store)
            return context
        if context.status != AWAITING_APPROVAL:
            raise ValueError("Only a run that is waiting for approval can be resumed.")
        context.status = RUNNING
        return self._run_agents(context, agents, store)

    def _run_agents(self, context: ProjectContext, agents: list["Agent"], store: RunStore | None) -> ProjectContext:
        done = self._done_agents(context)
        for index, agent in enumerate(agents):
            if (agent.name, agent.action) in done:
                continue
            checkpoint = getattr(agent, "checkpoint", None)
            if checkpoint and not self._approved(context, checkpoint):
                title = getattr(agent, "checkpoint_title", "Approval needed")
                context.record("Manager", f"Checkpoint {checkpoint}: {title}", WAITING, agent.preview(context))
                context.status = AWAITING_APPROVAL
                self.log(f"[Manager] Waiting for your approval at checkpoint {checkpoint} before {agent.name}.")
                self._save(context, store)
                return context

            self.log(f"[Manager] Handing off to {agent.handoff_label}...")
            start = time.perf_counter()
            try:
                result = agent.run(context)
            except Exception as exc:
                context.record(agent.name, agent.action, FAILED, f"{type(exc).__name__}: {exc}",
                               _elapsed(start))
                return self._finish(context, agents[index + 1:], store, failed=True)

            for attempt in result.attempts:
                context.record(agent.name, attempt.action, attempt.status, attempt.detail)
            forbidden = sorted(set(result.updates) - _UPDATABLE)
            if forbidden:
                context.record(agent.name, agent.action, FAILED,
                               f"Tried to change {', '.join(forbidden)}, which agents may not set.", _elapsed(start))
                return self._finish(context, agents[index + 1:], store, failed=True)
            for key, value in result.updates.items():
                setattr(context, key, value)
            context.notes.update(result.notes)
            context.record(agent.name, agent.action, result.status, result.detail, _elapsed(start))
            self.log(f"[Manager] {agent.name}: {result.status} ({result.detail}).")
            self._save(context, store)
            if result.status == FAILED:
                return self._finish(context, agents[index + 1:], store, failed=True)

        return self._finish(context, [], store, failed=False)

    def _finish(self, context: ProjectContext, remaining: list["Agent"], store: RunStore | None,
                failed: bool) -> ProjectContext:
        for agent in remaining:
            context.record(agent.name, agent.action, SKIPPED, "Not reached: an earlier step failed")
        context.status = FAILED if failed else COMPLETED
        self._save(context, store)
        return context

    @staticmethod
    def _done_agents(context: ProjectContext) -> set[tuple[str, str]]:
        return {(e.agent, e.action) for e in context.events if e.status == DONE}

    @staticmethod
    def _skipped(context: ProjectContext, agent: "Agent") -> bool:
        return any(e.agent == agent.name and e.action == agent.action and e.status == SKIPPED for e in context.events)

    @staticmethod
    def _approved(context: ProjectContext, checkpoint: str) -> bool:
        return any(a.checkpoint == checkpoint and a.decision == APPROVED for a in context.approvals)

    @staticmethod
    def _save(context: ProjectContext, store: RunStore | None) -> None:
        if store is not None:
            store.save(context)


def _elapsed(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)
