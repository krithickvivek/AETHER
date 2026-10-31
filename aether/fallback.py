"""FallbackPlanner: try one planner, and use another if the first one fails."""

from aether.models import Plan, TaskBrief
from aether.planner import Planner


class FallbackPlanner(Planner):
    """Tries `primary` first. On any error, uses `fallback` and records why.

    plan.planner_name always names the planner that really produced the plan.
    """

    def __init__(self, primary: Planner, fallback: Planner):
        self.primary = primary
        self.fallback = fallback
        self.name = primary.name
        self.fallback_reason: str | None = None

    def create_plan(self, brief: TaskBrief) -> Plan:
        self.fallback_reason = None
        try:
            return self.primary.create_plan(brief)
        except Exception as exc:
            self.fallback_reason = str(exc) or type(exc).__name__
            return self.fallback.create_plan(brief)
