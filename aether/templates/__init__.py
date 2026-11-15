"""Project templates used by the Developer Agent.

Each template is a set of tested source files stored next to this module as
"<path>.tmpl" (so they are never imported or run as part of AETHER). The Developer
copies them into a run's Workspace. Templates use only Python's standard library,
so generated projects run without installing anything.
"""

import re
from dataclasses import dataclass, field
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
    domain_patterns: tuple   # words that name this project type; at least one must match to select it
    files: tuple
    run_command: str
    test_command: str = "python -m unittest discover -v"
    hint_patterns: tuple = ()  # related words that support a match but never select the template alone

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
        domain_patterns=(r"\bcalculators?\b", r"\barithmetic\b", r"\bcalc\b"),
        hint_patterns=(r"\bcalculat(?:e|es|ed|ing|ions?)\b",),
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
        # "to do" on its own is too common ("a website to do shopping"), so the spaced form needs "list".
        domain_patterns=(r"\bto-?dos?\b", r"\bto do lists?\b", r"\bchecklists?\b",
                         r"\btask (?:lists?|managers?|trackers?)\b"),
        hint_patterns=(r"\btasks?\b",),
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
        domain_patterns=(r"\blibrar(?:y|ies|ians?)\b",),
        hint_patterns=(r"\bbooks?\b", r"\bborrow\w*", r"\bloans?\b"),
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


# How a template choice was reached.
SELECTED = "selected"      # one template clearly fits
OVERRIDDEN = "overridden"  # the user explicitly chose a template that does not fully fit
AMBIGUOUS = "ambiguous"    # the requirement names more than one project type
POOR_FIT = "poor_fit"      # it names one project type, but most requested features are unsupported
NO_MATCH = "no_match"      # it names no supported project type


def _matches(patterns: tuple, text: str) -> list[str]:
    """The distinct words in the text that match any of the patterns, in order of appearance."""
    found: dict[str, int] = {}
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            found.setdefault(match.group(0), match.start())
    return sorted(found, key=found.get)


@dataclass
class TemplateFit:
    """How well one template fits a requirement.

    Only words that name the project type (evidence) can make a template eligible. Related
    words (hints) and detected features never do: "book", "tasks", "save" or "account" alone
    say nothing about which kind of project is wanted.
    """

    template: ProjectTemplate
    evidence: list[str]     # words naming this project type, e.g. "to-do"
    hints: list[str]        # related words, e.g. "tasks"; not enough on their own
    supported: list[str]    # requested features this template implements
    unsupported: list[str]  # requested features it does not

    @property
    def names_project_type(self) -> bool:
        return bool(self.evidence)

    @property
    def substantial_gap(self) -> bool:
        """More requested features are missing than provided, so the result would not be what was asked for."""
        return len(self.unsupported) > len(self.supported)

    @property
    def suitable(self) -> bool:
        return self.names_project_type and not self.substantial_gap


def evaluate_fit(template: ProjectTemplate, brief: TaskBrief) -> TemplateFit:
    text = brief.requirement.lower()
    features = set(brief.features)
    return TemplateFit(
        template=template,
        evidence=_matches(template.domain_patterns, text),
        hints=_matches(template.hint_patterns, text),
        supported=sorted(features & template.supports),
        unsupported=sorted(features - template.supports),
    )


@dataclass
class TemplateChoice:
    template: ProjectTemplate | None
    unsupported: list[str]    # detected features this template does not implement
    reason: str
    status: str = NO_MATCH
    supported: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)  # words that name the chosen project type
    hints: list[str] = field(default_factory=list)     # related words that were found
    candidates: list[ProjectTemplate] = field(default_factory=list)  # templates an explicit override may use

    @property
    def can_override(self) -> bool:
        return self.template is None and bool(self.candidates)


def _quoted(words: list[str]) -> str:
    return ", ".join(f'"{w}"' for w in words)


def _not_covered(unsupported: list[str]) -> str:
    return f" Not covered by this template: {', '.join(unsupported)}." if unsupported else ""


def choose_template(brief: TaskBrief, override: str | None = None) -> TemplateChoice:
    """Pick the template that clearly fits the requirement, or explain why none is chosen.

    A template is chosen automatically only when the requirement names its project type, no
    other project type is named, and it supports at least as many requested features as it
    lacks. Otherwise template is None; if a named project type exists, `candidates` lists the
    templates the user may still pick through an explicit override (passed back as `override`).
    """
    fits = [evaluate_fit(template, brief) for template in TEMPLATES]
    named = [fit for fit in fits if fit.names_project_type]
    features = sorted(set(brief.features))

    if override:
        fit = next((f for f in named if f.template.key == override), None)
        if fit is None:
            return TemplateChoice(None, features, f"The override {override!r} is not allowed: the requirement does "
                                                  "not name that project type.", NO_MATCH)
        return TemplateChoice(
            fit.template, fit.unsupported,
            f"Explicit override: the user chose the {fit.template.title} template although it does not fully fit "
            f"this requirement.{_not_covered(fit.unsupported)}",
            OVERRIDDEN, fit.supported, fit.evidence, fit.hints,
        )

    if not named:
        reason = ("No supported project type matches this requirement. "
                  f"Supported types: {'; '.join(t.title for t in TEMPLATES)}.")
        hints = [word for fit in fits for word in fit.hints]
        if hints:
            reason += f" Related words ({_quoted(hints)}) were found, but they do not identify a project type on their own."
        return TemplateChoice(None, features, reason, NO_MATCH, hints=hints)

    if len(named) > 1:
        types = " and ".join(f"{fit.template.title} ({_quoted(fit.evidence)})" for fit in named)
        return TemplateChoice(None, features,
                              f"The requirement names more than one project type: {types}. "
                              "AETHER will not guess which one is meant.",
                              AMBIGUOUS, evidence=[w for fit in named for w in fit.evidence],
                              candidates=[fit.template for fit in named])

    fit = named[0]
    if fit.substantial_gap:
        supported = ", ".join(fit.supported) or "none"
        return TemplateChoice(None, fit.unsupported,
                              f"The requirement names a {fit.template.title} ({_quoted(fit.evidence)}), but that "
                              f"template does not support most of the requested features "
                              f"(not supported: {', '.join(fit.unsupported)}; supported: {supported}).",
                              POOR_FIT, fit.supported, fit.evidence, fit.hints, candidates=[fit.template])

    return TemplateChoice(fit.template, fit.unsupported,
                          f"Matched the {fit.template.title} template.{_not_covered(fit.unsupported)}",
                          SELECTED, fit.supported, fit.evidence, fit.hints)


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
