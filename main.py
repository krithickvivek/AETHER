"""AETHER command-line entry point.

Usage:
    python main.py "Build a to-do website where users can log in and save tasks"
    python main.py            (you will be asked to type the requirement)

The planner is chosen with the AETHER_PLANNER setting (rule by default). See aether/config.py.
"""

import sys

from aether.config import build_planner
from aether.fallback import FallbackPlanner
from aether.manager import ManagerAgent
from aether.models import Plan
from aether.planner import RuleBasedPlanner


def read_requirement(args: list[str]) -> str:
    """Take the requirement from the command line, or ask the user for it."""
    if args:
        return " ".join(args)
    try:
        return input("Enter a software requirement: ")
    except EOFError:
        return ""


def print_plan(plan: Plan) -> None:
    """Print the plan grouped by phase, with continuous step numbers."""
    print()
    print("=" * 60)
    print(f"DEVELOPMENT PLAN (by {plan.planner_name})")
    print(f"Requirement: {plan.requirement}")
    print("=" * 60)

    current_phase = None
    for number, step in enumerate(plan.steps, start=1):
        if step.phase != current_phase:
            current_phase = step.phase
            print(f"\n{current_phase}")
        print(f"  {number}. {step.description}")
    print()


def main(args: list[str] | None = None) -> int:
    args = sys.argv[1:] if args is None else args
    requirement = read_requirement(args)

    try:
        planner = build_planner()
    except ValueError as error:
        print(f"Configuration error: {error}")
        return 1
    if isinstance(planner, FallbackPlanner):
        print("[AETHER] Experimental local AI planner enabled. This can take a few minutes.")
    manager = ManagerAgent(planner=planner)

    try:
        plan = manager.run(requirement)
    except ValueError as error:
        print(f"Error: {error}")
        return 1

    if isinstance(planner, FallbackPlanner) and planner.fallback_reason:
        print(f"[AETHER] Local AI planner failed: {planner.fallback_reason}")
        print(f"[AETHER] Used {RuleBasedPlanner.name} instead.")
    print_plan(plan)
    if plan.planner_name != RuleBasedPlanner.name:
        print("Note: this plan was written by a small local AI model. Review it before relying on it.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
