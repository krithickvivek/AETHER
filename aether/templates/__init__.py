"""Project templates used by the Developer Agent.

Each template is a set of tested source files stored next to this module as
"<path>.tmpl" (so they are never imported or run as part of AETHER). The Developer
copies them into a run's Workspace. Templates use only Python's standard library,
so generated projects run without installing anything.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from aether.models import Plan, TaskBrief
from aether.ollama_planner import mentions_feature

TEMPLATE_ROOT = Path(__file__).resolve().parent
FEATURES = ("authentication", "database", "web_ui", "api")
REQ, DES, SET, IMP, TST, DOC = "Requirements", "Design", "Setup", "Implementation", "Testing", "Documentation"
CORE = "core"


@dataclass(frozen=True)
class TemplateFile:
    path: str                       # where the file goes in the generated project
    phases: frozenset = frozenset()  # plan phases this file carries out
    features: frozenset = frozenset()  # feature keys it implements, or "core"


def tf(path: str, phases: set, features: set) -> TemplateFile:
    return TemplateFile(path, frozenset(phases), frozenset(features))


@dataclass(frozen=True)
class ProjectTemplate:
    key: str
    title: str
    kind: str
    description: str
    supports: frozenset
    domain_patterns: tuple
    files: tuple
    run_command: str
    test_command: str = "python -m unittest discover -v"

    def source(self, path: str) -> Path:
        return TEMPLATE_ROOT / self.key / (path + ".tmpl")

    def render(self, brief: TaskBrief) -> dict[str, str]:
        """The generated project's files: {path: text}. Only README text is personalised."""
        values = {"{{REQUIREMENT}}": brief.requirement, "{{TITLE}}": self.title}
        rendered = {}
        for file in self.files:
            text = self.source(file.path).read_text(encoding="utf-8")
            for placeholder, value in values.items():
                text = text.replace(placeholder, value)
            rendered[file.path] = text
        return rendered


DOCS = {REQ, DES, SET, DOC}

TEMPLATES = (
    ProjectTemplate(
        key="calculator",
        title="Command-line calculator",
        kind="Python command-line app",
        description="Adds, subtracts, multiplies and divides from the command line or an interactive prompt.",
        supports=frozenset(),
        domain_patterns=(r"\bcalculat\w*", r"\barithmetic\b", r"\bcalc\b"),
        files=(
            tf("calculator.py", {IMP}, {CORE}),
            tf("tests/__init__.py", set(), set()),
            tf("tests/test_calculator.py", {TST}, {CORE}),
            tf("README.md", DOCS, {CORE}),
        ),
        run_command="python calculator.py 12 + 30",
    ),
    ProjectTemplate(
        key="todo_web",
        title="To-do web app",
        kind="Python web app (standard library) with SQLite",
        description="Users register and log in (salted PBKDF2 password hashes), then add, complete and delete their own tasks.",
        supports=frozenset({"authentication", "database", "web_ui"}),
        domain_patterns=(r"\bto-?do\b", r"\bto do\b", r"\btasks?\b", r"\bchecklist\b"),
        files=(
            tf("server.py", {SET, IMP}, {CORE, "web_ui"}),
            tf("todo_app/__init__.py", set(), set()),
            tf("todo_app/schema.sql", {DES, SET, IMP}, {"database"}),
            tf("todo_app/db.py", {SET, IMP}, {"database"}),
            tf("todo_app/auth.py", {IMP}, {"authentication"}),
            tf("todo_app/tasks.py", {IMP}, {CORE, "database"}),
            tf("todo_app/web.py", {IMP}, {CORE, "web_ui"}),
            tf("todo_app/static/style.css", {DES, IMP}, {"web_ui"}),
            tf("tests/__init__.py", set(), set()),
            tf("tests/test_auth.py", {TST}, {"authentication"}),
            tf("tests/test_tasks.py", {TST}, {CORE, "database"}),
            tf("tests/test_web.py", {TST}, {"web_ui", "authentication"}),
            tf("README.md", DOCS, {CORE, "authentication", "database", "web_ui"}),
        ),
        run_command="python server.py",
    ),
    ProjectTemplate(
        key="library_api",
        title="Library management REST API",
        kind="Python JSON REST API (standard library) with SQLite",
        description="Manages books, members and loans through JSON endpoints, with validation and availability checks.",
        supports=frozenset({"database", "api"}),
        domain_patterns=(r"\blibrar(?:y|ies)\b", r"\bbooks?\b", r"\bborrow\w*", r"\bloans?\b"),
        files=(
            tf("server.py", {SET, IMP}, {CORE, "api"}),
            tf("library_api/__init__.py", set(), set()),
            tf("library_api/schema.sql", {DES, SET, IMP}, {"database"}),
            tf("library_api/db.py", {SET, IMP}, {"database"}),
            tf("library_api/store.py", {IMP}, {CORE, "database"}),
            tf("library_api/api.py", {IMP}, {CORE, "api"}),
            tf("tests/__init__.py", set(), set()),
            tf("tests/test_store.py", {TST}, {CORE, "database"}),
            tf("tests/test_api.py", {TST}, {CORE, "api"}),
            tf("README.md", DOCS, {CORE, "database", "api"}),
        ),
        run_command="python server.py",
    ),
)


@dataclass
class TemplateChoice:
    template: ProjectTemplate | None
    unsupported: list[str]    # detected features this template does not implement
    reason: str


def choose_template(brief: TaskBrief) -> TemplateChoice:
    """Pick the supported template that best fits the requirement, or explain why none does."""
    text = brief.requirement.lower()
    features = set(brief.features)
    best, best_score = None, None
    for template in TEMPLATES:
        hits = sum(1 for pattern in template.domain_patterns if re.search(pattern, text))
        if not hits:
            continue
        score = hits * 3 + len(features & template.supports) - 2 * len(features - template.supports)
        if best_score is None or score > best_score:
            best, best_score = template, score
    if best is None:
        supported = "; ".join(t.title for t in TEMPLATES)
        return TemplateChoice(None, sorted(features),
                              f"No supported project type matches this requirement. Supported types: {supported}.")
    unsupported = sorted(features - best.supports)
    reason = f"Matched the {best.title} template."
    if unsupported:
        reason += f" Not covered by this template: {', '.join(unsupported)}."
    return TemplateChoice(best, unsupported, reason)


def map_coverage(plan: Plan, template: ProjectTemplate) -> tuple[dict[str, list[int]], list[int]]:
    """Which plan steps (1-based numbers) each file carries out, and which steps no file covers."""
    covers: dict[str, list[int]] = {file.path: [] for file in template.files}
    uncovered = []
    for number, step in enumerate(plan.steps, start=1):
        step_features = {f for f in FEATURES if mentions_feature(f, step)}
        hit = False
        for file in template.files:
            if step.phase not in file.phases:
                continue
            if (step_features & file.features) if step_features else CORE in file.features:
                covers[file.path].append(number)
                hit = True
        if not hit:
            uncovered.append(number)
    return covers, uncovered
