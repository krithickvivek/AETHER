"""Data shared between agents.

Agents talk to each other only through these simple objects, so any agent
(rule-based today, AI-powered later) can be swapped without changing the others.
"""

from dataclasses import dataclass, field


@dataclass
class TaskBrief:
    """What the Manager hands to the Planner."""

    requirement: str
    features: list[str] = field(default_factory=list)


@dataclass
class PlanStep:
    """One step of a development plan."""

    phase: str
    description: str


@dataclass
class Plan:
    """What the Planner hands back to the Manager."""

    requirement: str
    planner_name: str
    steps: list[PlanStep] = field(default_factory=list)
