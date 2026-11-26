"""Turn core objects (ProjectContext, RunResult, DevelopmentPreview) into the API's response models.

No agent logic and no decisions about the run are made here. A few small presentation rules that currently exist
only inside app.py (which cannot be imported without starting Streamlit) are repeated and marked DUPLICATED, so
that a later shared module can replace them: display_path, fit_badge, the failure wording, the override
consequences, the example requirements and the planner labels.
"""

import re
from pathlib import Path

from aether.context import AWAITING_APPROVAL, FAILED, SOURCE_ROOT, ProjectContext
from aether.developer import DevelopmentPreview, approved_override, preview
from aether.ollama_planner import DEFAULT_MODEL
from aether.planner import RuleBasedPlanner
from aether.templates import AMBIGUOUS, POOR_FIT, ProjectTemplate
from aether_api import schemas
from aether_ui.workflow import (
    FEATURE_LABELS, RunResult, group_by_phase, progress_steps, step_origin,
)

LANGUAGES = {"py": "python", "sql": "sql", "md": "markdown", "css": "css", "html": "html", "json": "json"}  # DUPLICATED (app.py)
PLANNER_LABELS = {"rule": "Rule-based (default)", "ollama": f"Local AI: {DEFAULT_MODEL} (experimental)"}  # DUPLICATED (app.py)
EXAMPLES = [  # DUPLICATED (app.py)
    ("To-do web app", "Build a to-do website where users can log in and save their tasks"),
    ("Library REST API", "Create a REST API for a library that stores books and members in a database"),
    ("CLI calculator", "A command-line calculator for basic arithmetic"),
]
REQUIREMENT_MAX_CHARS = 2000  # DUPLICATED (app.py: text_area max_chars)
NO_RUN_STORED = "This saved plan has no stored run, so it cannot be approved."
RUN_NOT_LOADABLE = "The stored run for this plan could not be loaded, so it cannot be approved."
RUNS_FOLDER_MESSAGE = ("Generated projects cannot be saved: runs cannot be stored inside AETHER's own folder. "
                       "Choose a folder outside it (AETHER_RUNS_DIR).")
NOT_WAITING = "This run is not waiting for approval."


# ------------------------------------------------------------------ privacy

def _patterns() -> list[tuple[re.Pattern, str]]:
    pairs = []
    for base, replacement in ((SOURCE_ROOT, "<AETHER>"), (Path.home(), "~")):
        for variant in {str(base), str(base).replace("\\", "/")}:
            pairs.append((re.compile(re.escape(variant), re.IGNORECASE), replacement))
    return pairs


def scrub(text: str | None) -> str | None:
    """Hide this computer's home folder and AETHER's own folder in any text that came from saved data or an
    exception message, so a response never carries an absolute path or the user name."""
    if not text:
        return text
    for pattern, replacement in _patterns():
        text = pattern.sub(replacement, text)
    return text


def display_path(path: Path) -> str:  # DUPLICATED (app.py)
    path = Path(path)
    try:
        relative = path.resolve().relative_to(Path.home().resolve())
        if len(relative.parts) <= 4:
            return "~/" + relative.as_posix()
    except ValueError:
        pass
    return "…/" + "/".join(path.parts[-3:])


# ------------------------------------------------------------------ small pieces

def feature_refs(features: list[str]) -> list[schemas.FeatureRef]:
    return [schemas.FeatureRef(key=f, label=FEATURE_LABELS.get(f, f)) for f in features]


def language_of(path: str) -> str | None:
    return LANGUAGES.get(path.rsplit(".", 1)[-1])


def file_infos(context: ProjectContext) -> list[schemas.FileInfo]:
    return [schemas.FileInfo(path=f.path, size=f.size, sha256=f.sha256, author=f.author, covers=list(f.covers),
                             language=language_of(f.path)) for f in context.files]


def template_info(template: ProjectTemplate) -> schemas.TemplateInfo:
    return schemas.TemplateInfo(key=template.key, title=template.title, kind=template.kind,
                                description=template.description)


def fit_badge(plan_preview: DevelopmentPreview) -> schemas.Badge:  # DUPLICATED (app.py fit_badge)
    choice = plan_preview.choice
    if plan_preview.template is not None:
        return schemas.Badge(label="Partial fit", tone="warn") if choice.unsupported \
            else schemas.Badge(label="Fits the requirement", tone="ok")
    if choice.status == POOR_FIT:
        return schemas.Badge(label="Poor fit", tone="fail")
    if choice.status == AMBIGUOUS:
        return schemas.Badge(label="Ambiguous: more than one project type", tone="warn")
    return schemas.Badge(label="No matching template", tone="fail")


# ------------------------------------------------------------------ actions (eligibility only in this phase)

def compute_actions(context: ProjectContext | None, plan_preview: DevelopmentPreview | None, folder_valid: bool,
                    has_run_id: bool = True) -> schemas.Actions:
    """What Checkpoint A would allow, mirroring the buttons app.py disables. A reason is always given when
    approval is unavailable, and it stays the same on every request (so an invalid runs folder is not a one-off
    message)."""
    if context is None:
        reason = RUNS_FOLDER_MESSAGE if not folder_valid else NO_RUN_STORED if not has_run_id else RUN_NOT_LOADABLE
        return schemas.Actions(can_approve=False, can_reject=False, can_override=False, disabled_reason=reason)
    if context.status != AWAITING_APPROVAL or plan_preview is None:
        return schemas.Actions(can_approve=False, can_reject=False, can_override=False, disabled_reason=NOT_WAITING)
    if not folder_valid:
        return schemas.Actions(can_approve=False, can_reject=False, can_override=False,
                               disabled_reason=RUNS_FOLDER_MESSAGE)
    can_approve = plan_preview.template is not None
    return schemas.Actions(
        can_approve=can_approve, can_reject=True, can_override=plan_preview.choice.can_override,
        disabled_reason=None if can_approve else plan_preview.choice.reason)


# ------------------------------------------------------------------ run view

def synthesize_result(context: ProjectContext, snapshot: RunResult | None) -> RunResult:
    """A minimal RunResult for progress_steps (it only reads the brief and the plan). Run time and the Manager log
    are NOT invented: they come from a history snapshot when there is one."""
    return RunResult(requirement=context.requirement, started_at=context.created_at,
                     duration_ms=snapshot.duration_ms if snapshot else 0.0,
                     stages=[], log=[], brief=context.brief, plan=context.plan, run_id=context.run_id)


def describe_failure(context: ProjectContext) -> tuple[str | None, str | None]:  # DUPLICATED (workflow.start_project)
    failed = next((e for e in context.events if e.status == FAILED and e.agent in ("Manager", "Planner")
                   and e.action != "Build plan with local AI model"), None)
    if failed is None:
        return None, None
    if failed.agent == "Manager":
        return scrub(failed.detail), None
    return "Something went wrong while generating the plan.", scrub(failed.detail)


def developer_error(context: ProjectContext) -> str | None:
    event = next((e for e in reversed(context.events) if e.agent == "Developer"), None)
    return scrub(event.detail) if event is not None and event.status == FAILED else None


def plan_view(plan) -> schemas.PlanView | None:
    if plan is None:
        return None
    numbered = list(enumerate(plan.steps, start=1))
    phases = [schemas.PhaseView(name=name, step_numbers=[n for n, step in numbered if step.phase == name])
              for name in group_by_phase(plan.steps)]
    steps = [schemas.PlanStepView(number=n, phase=s.phase, description=s.description,
                                  origin=step_origin(s, plan.planner_name)) for n, s in numbered]
    return schemas.PlanView(planner_name=plan.planner_name, ai_generated=plan.planner_name != RuleBasedPlanner.name,
                            steps=steps, phases=phases)


def run_view(*, source: str, result: RunResult, context: ProjectContext | None, context_state: str,
             folder_valid: bool, entry_id: str | None = None, snapshot: RunResult | None = None,
             workspace_exists: bool = False, workspace_display: str | None = None) -> schemas.RunView:
    """The one response model for a stored run (source "run") and a history snapshot (source "snapshot").

    `snapshot` supplies the run time and Manager log for a stored run; for source "snapshot" it is `result`."""
    has_snapshot = snapshot is not None
    plan_preview = preview(context) if context is not None and context.brief is not None \
        and context.plan is not None else None
    if source == "run":
        error, error_details = describe_failure(context)
    else:
        error, error_details = scrub(result.error), scrub(result.error_details)

    notes = {key: scrub(value) for key, value in (context.notes if context else {}).items()}
    fallback = notes.get("planner_fallback") or scrub(snapshot.fallback_reason if snapshot else None)
    workspace = None
    if context is not None and folder_valid and workspace_display is not None:
        workspace = schemas.WorkspaceInfo(display_path=workspace_display, exists=workspace_exists)
    plan = plan_view(result.plan)
    return schemas.RunView(
        source=source, run_id=context.run_id if context else result.run_id, entry_id=entry_id,
        context_state=context_state, status=context.status if context else None,
        requirement=result.requirement, started_at=result.started_at,
        duration_ms=snapshot.duration_ms if has_snapshot else None,
        ok=result.plan is not None, error=error, error_details=error_details,
        developer_error=developer_error(context) if context else None,
        brief=None if result.brief is None else schemas.Brief(
            requirement=result.brief.requirement, features=feature_refs(result.brief.features)),
        plan=plan, ai_generated=bool(plan and plan.ai_generated), fallback_reason=fallback,
        project_type=context.project_type if context else None, template_title=notes.get("developer_template"),
        progress=[schemas.ProgressStepView(title=s.title, status=s.status, detail=s.detail)
                  for s in progress_steps(result, context)],
        events=None if context is None else [
            schemas.EventView(time=e.time, agent=e.agent, action=e.action, status=e.status, detail=scrub(e.detail) or "",
                              duration_ms=e.duration_ms) for e in context.events],
        approvals=[] if context is None else [
            schemas.ApprovalView(checkpoint=a.checkpoint, decision=a.decision, time=a.time, note=scrub(a.note) or "",
                                 choice=a.choice) for a in context.approvals],
        notes=notes, files=[] if context is None else file_infos(context),
        workspace=workspace,
        actions=compute_actions(context, plan_preview, folder_valid, has_run_id=bool(result.run_id)),
        warnings=[] if context is None else [scrub(w) or "" for w in context.load_warnings],
        stages=None if context is not None or source == "run" else
        [schemas.StageView(agent=s.agent, title=s.title, status=s.status, detail=scrub(s.detail) or "")
         for s in result.stages],
        manager_log=[scrub(line) or "" for line in snapshot.log] if has_snapshot else None,
    )


# ------------------------------------------------------------------ checkpoint preview

def _override_consequences(forced: DevelopmentPreview, title: str) -> list[str]:  # DUPLICATED (app.py render_override)
    lines = [f"The project will be a {title}, whatever the requirement asks for."]
    if forced.choice.unsupported:
        names = ", ".join(FEATURE_LABELS.get(f, f) for f in forced.choice.unsupported)
        lines.append(f"These requested features will not be built: {names}.")
    if forced.uncovered_steps:
        lines.append(f"Plan steps {', '.join(map(str, forced.uncovered_steps))} will have no file.")
    lines.append("The run is recorded as an explicit override in the activity log.")
    return lines


class InvalidOverride(ValueError):
    pass


def checkpoint_preview(context: ProjectContext, folder_valid: bool, override: str | None) -> schemas.CheckpointPreview:
    """Checkpoint A as it would be shown: a pure function of the loaded run (nothing is written or changed)."""
    if context.brief is None or context.plan is None:
        return schemas.CheckpointPreview(
            run_id=context.run_id, available=False, reason="This run has no plan to preview.", status=context.status,
            awaiting_approval=False, template=None, fit=None, planned_files=[], uncovered_steps=[], candidates=[],
            override=None, actions=compute_actions(context, None, folder_valid))
    awaiting = context.status == AWAITING_APPROVAL
    plan_preview = preview(context, None if awaiting else approved_override(context))
    choice = plan_preview.choice
    override_preview = None
    if override:
        keys = {t.key: t for t in choice.candidates}
        if choice.template is not None or override not in keys:
            raise InvalidOverride("That template cannot be chosen for this requirement.")
        override_preview = schemas.OverridePreview(
            key=override, title=keys[override].title,
            consequences=_override_consequences(preview(context, override), keys[override].title))
    return schemas.CheckpointPreview(
        run_id=context.run_id, available=True, reason=None, status=context.status, awaiting_approval=awaiting,
        template=None if plan_preview.template is None else template_info(plan_preview.template),
        fit=schemas.FitInfo(status=choice.status, reason=scrub(choice.reason) or "", badge=fit_badge(plan_preview),
                            evidence=list(choice.evidence), hints=list(choice.hints),
                            supported=feature_refs(choice.supported), unsupported=feature_refs(choice.unsupported)),
        planned_files=[schemas.PlannedFile(path=p, covers=list(plan_preview.covers.get(p, [])))
                       for p in plan_preview.files],
        uncovered_steps=list(plan_preview.uncovered_steps),
        candidates=[template_info(t) for t in choice.candidates],
        override=override_preview, actions=compute_actions(context, plan_preview, folder_valid))
