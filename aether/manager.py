"""Manager agent: understands a requirement and coordinates the workflow."""

import re
from typing import Callable

from aether.models import Plan, TaskBrief
from aether.planner import Planner

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
