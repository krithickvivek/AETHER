"""Manager agent: understands a requirement and coordinates the workflow."""

import time
from dataclasses import fields
from typing import Callable

from aether.agents import Agent, AgentResult, PlannerAgent
from aether.context import (
    APPROVED, AWAITING_APPROVAL, CANCELLED, COMPLETED, DONE, FAILED, REJECTED, RUNNING, SKIPPED, WAITING,
    Approval, ProjectContext, RunStore, now,
)
from aether.feature_detection import FEATURE_KEYWORDS, detect_features  # noqa: F401 (re-exported)
from aether.models import Plan, TaskBrief
from aether.planner import Planner

# ProjectContext fields an agent may set through AgentResult.updates.
_UPDATABLE = {f.name for f in fields(ProjectContext)} - {
    "run_id", "requirement", "created_at", "status", "events", "approvals"}

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
        """Plan only: a thin compatibility wrapper over run_pipeline() (the one execution path).

        Runs the pipeline with just this Manager's planner and returns the plan. It behaves as it always
        did: an unusable requirement raises ValueError, an exception from the planner is raised unchanged,
        and the log shows the same four messages."""
        agent = _PlanOnlyAgent(self.planner)
        # The pipeline's own "[Manager] Planner: done (...)" line is replaced by the plan-only summary below.
        pipeline_log, self.log = self.log, lambda message: (
            None if message.startswith(f"[Manager] {agent.name}: ") else pipeline_log(message))
        try:
            context = self.run_pipeline(requirement, [agent])
        finally:
            self.log = pipeline_log
        if context.plan is None:
            if agent.error is not None:
                raise agent.error
            failure = next((e for e in context.events if e.status == FAILED), None)
            raise ValueError(failure.detail if failure else "The Manager could not produce a plan.")
        self.log(f"[Manager] Received a plan with {len(context.plan.steps)} steps from {context.plan.planner_name}.")
        return context.plan

    def run_pipeline(self, requirement: str, agents: list[Agent], store: RunStore | None = None) -> ProjectContext:
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

    def resume(self, context: ProjectContext, agents: list[Agent], store: RunStore | None = None) -> ProjectContext:
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

    def _run_agents(self, context: ProjectContext, agents: list[Agent], store: RunStore | None) -> ProjectContext:
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

    def _finish(self, context: ProjectContext, remaining: list[Agent], store: RunStore | None,
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
    def _skipped(context: ProjectContext, agent: Agent) -> bool:
        return any(e.agent == agent.name and e.action == agent.action and e.status == SKIPPED for e in context.events)

    @staticmethod
    def _approved(context: ProjectContext, checkpoint: str) -> bool:
        return any(a.checkpoint == checkpoint and a.decision == APPROVED for a in context.approvals)

    @staticmethod
    def _save(context: ProjectContext, store: RunStore | None) -> None:
        if store is not None:
            store.save(context)


class _PlanOnlyAgent(PlannerAgent):
    """The planner step used by ManagerAgent.run(): remembers an exception so run() can raise it unchanged."""

    def __init__(self, planner: Planner):
        super().__init__(planner)
        self.error: Exception | None = None

    def run(self, context: ProjectContext) -> AgentResult:
        try:
            return super().run(context)
        except Exception as exc:
            self.error = exc
            raise


def _elapsed(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)
