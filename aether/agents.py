"""The common interface for AETHER's specialist agents.

An agent receives the shared ProjectContext and returns an AgentResult. It does not change
the context itself: the Manager applies the result's updates and records the events, so
every step of a run is logged in one place.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from aether.context import DONE, FAILED, ProjectContext
from aether.planner import Planner


@dataclass
class Attempt:
    """Something an agent tried that did not produce its result (e.g. an AI model that fell back)."""

    action: str
    status: str
    detail: str


@dataclass
class AgentResult:
    status: str                      # DONE or FAILED
    detail: str = ""
    updates: dict = field(default_factory=dict)   # ProjectContext fields to set, e.g. {"plan": plan}
    notes: dict[str, str] = field(default_factory=dict)
    attempts: list[Attempt] = field(default_factory=list)


class Agent(ABC):
    name = "Agent"       # role shown in the activity log, e.g. "Planner"
    action = "Work"      # what the agent does, e.g. "Build development plan"

    @abstractmethod
    def run(self, context: ProjectContext) -> AgentResult:
        """Do this agent's job for the project and report what happened."""


class PlannerAgent(Agent):
    """Adapts any Planner (rule-based, Ollama, or with fallback) to the Agent interface."""

    name = "Planner"
    action = "Build development plan"

    def __init__(self, planner: Planner):
        self.planner = planner

    def run(self, context: ProjectContext) -> AgentResult:
        if context.brief is None:
            return AgentResult(FAILED, "No task brief from the Manager.")
        plan = self.planner.create_plan(context.brief)
        result = AgentResult(DONE, f"{len(plan.steps)} steps from {plan.planner_name}", updates={"plan": plan})
        fallback_reason = getattr(self.planner, "fallback_reason", None)
        if fallback_reason:
            result.notes["planner_fallback"] = fallback_reason
            result.attempts.append(Attempt("Build plan with local AI model", FAILED, fallback_reason))
        return result
