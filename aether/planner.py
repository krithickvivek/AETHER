"""Planner agents: turn a TaskBrief into an ordered development Plan."""

from abc import ABC, abstractmethod

from aether.models import Plan, PlanStep, TaskBrief

# Every plan is ordered by these phases.
PHASES = ["Requirements", "Design", "Setup", "Implementation", "Testing", "Documentation"]

# Steps that appear in every plan, whatever the requirement.
BASE_STEPS = [
    ("Requirements", "Clarify the goal, target users, and scope of the requirement"),
    ("Requirements", "List the functional and non-functional requirements"),
    ("Design", "Choose the technology stack"),
    ("Design", "Design the overall architecture and main modules"),
    ("Setup", "Create the project structure and set up version control (git)"),
    ("Implementation", "Implement the core functionality described in the requirement"),
    ("Testing", "Write and run unit tests for the core functionality"),
    ("Documentation", "Write a README with setup and usage instructions"),
]

# Extra steps added when the Manager detects a feature (see feature_detection.FEATURE_KEYWORDS).
FEATURE_STEPS = {
    "authentication": [
        ("Design", "Design user accounts and roles"),
        ("Implementation", "Implement registration, login, and logout with hashed passwords"),
        ("Testing", "Test login with valid and invalid credentials"),
    ],
    "database": [
        ("Design", "Design the data model (entities, fields, relationships)"),
        ("Setup", "Set up the database and its connection settings"),
        ("Implementation", "Implement create, read, update, and delete (CRUD) operations"),
        ("Testing", "Test that data is saved and loaded correctly"),
    ],
    "web_ui": [
        ("Design", "Sketch the main pages and the user flow"),
        ("Implementation", "Build the web pages and connect them to the backend"),
        ("Testing", "Check the pages in a browser, including on a small screen"),
    ],
    "api": [
        ("Design", "Define the API endpoints and their request/response formats"),
        ("Implementation", "Implement the API endpoints with input validation"),
        ("Testing", "Test each API endpoint, including error responses"),
    ],
}


class Planner(ABC):
    """The interface every planner must follow.

    The Manager only depends on this class, so a future AI-powered planner
    can replace RuleBasedPlanner by implementing the same create_plan method.
    """

    name = "Planner"

    @abstractmethod
    def create_plan(self, brief: TaskBrief) -> Plan:
        """Return a development plan for the given brief."""


class RuleBasedPlanner(Planner):
    """Builds a plan from fixed rules: base steps plus steps for each detected feature."""

    name = "RuleBasedPlanner"

    def create_plan(self, brief: TaskBrief) -> Plan:
        raw_steps = list(BASE_STEPS)
        for feature in brief.features:
            raw_steps.extend(FEATURE_STEPS.get(feature, []))

        # sorted() is stable, so steps keep their original order within a phase.
        raw_steps.sort(key=lambda step: PHASES.index(step[0]))

        steps = [PlanStep(phase=phase, description=text) for phase, text in raw_steps]
        return Plan(requirement=brief.requirement, planner_name=self.name, steps=steps)
