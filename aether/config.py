"""Choose which planner AETHER uses.

Set the AETHER_PLANNER environment variable:
    rule    RuleBasedPlanner (default)
    ollama  Experimental OllamaPlanner (local llama3.2:1b), falling back to RuleBasedPlanner
"""

import os

from aether.fallback import FallbackPlanner
from aether.ollama_planner import OllamaPlanner
from aether.planner import Planner, RuleBasedPlanner

PLANNER_CHOICES = ("rule", "ollama")
DEFAULT_PLANNER = "rule"


def planner_setting() -> str:
    """The configured planner kind, from AETHER_PLANNER (default: rule)."""
    value = os.environ.get("AETHER_PLANNER", DEFAULT_PLANNER).strip().lower()
    if value not in PLANNER_CHOICES:
        raise ValueError(f"AETHER_PLANNER must be one of {', '.join(PLANNER_CHOICES)}, not {value!r}.")
    return value


def build_planner(kind: str | None = None) -> Planner:
    kind = kind or planner_setting()
    if kind == "rule":
        return RuleBasedPlanner()
    if kind == "ollama":
        return FallbackPlanner(primary=OllamaPlanner(), fallback=RuleBasedPlanner())
    raise ValueError(f"Unknown planner {kind!r}. Choose one of {', '.join(PLANNER_CHOICES)}.")
