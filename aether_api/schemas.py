"""The API contract: every request and response shape, as Pydantic models.

These are NOT the saved formats. context.json and history.json are written only by the AETHER core
(RunStore, aether_ui.history); the API never saves these models. They are explicit and flat enough for
TypeScript generation from /api/v1/openapi.json. Timestamps are the core's naive local ISO-8601 strings
(for example "2026-11-25T14:03:09"), passed through unchanged.
"""

from typing import Literal

from pydantic import BaseModel

HistoryFileState = Literal["ok", "missing", "damaged", "unreadable"]   # missing = no file yet (a healthy, empty history)
ContextState = Literal["ok", "missing", "unreadable"]
Tone = Literal["ok", "warn", "fail", "neutral"]
RunSource = Literal["run", "snapshot"]


# ------------------------------------------------------------------ errors

class ErrorBody(BaseModel):
    code: str
    message: str
    details: list | dict | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


# ------------------------------------------------------------------ health and config

class HealthResponse(BaseModel):
    status: Literal["ok"]
    api_version: str
    schema_version: int
    read_only: bool


class PlannerOption(BaseModel):
    key: str
    label: str


class ExampleRequirement(BaseModel):
    label: str
    text: str


class Limits(BaseModel):
    requirement_max_chars: int
    history_max_entries: int


class Locations(BaseModel):
    runs: str | None                 # a short ~/... form; null when the runs folder is not usable
    history: str
    runs_folder_valid: bool


class ConfigWarning(BaseModel):
    code: str
    message: str


class ConfigResponse(BaseModel):
    planners: list[PlannerOption]
    default_planner: str
    model: str
    limits: Limits
    examples: list[ExampleRequirement]
    locations: Locations
    warnings: list[ConfigWarning]


class OllamaStatusResponse(BaseModel):
    ready: bool
    message: str
    model: str


# ------------------------------------------------------------------ shared building blocks

class FeatureRef(BaseModel):
    key: str
    label: str


class Brief(BaseModel):
    requirement: str
    features: list[FeatureRef]


class PlanStepView(BaseModel):
    number: int                      # 1-based position in the plan
    phase: str
    description: str
    origin: str | None               # "core", a feature key, or null (see aether_ui.workflow.step_origin)


class PhaseView(BaseModel):
    name: str
    step_numbers: list[int]


class PlanView(BaseModel):
    planner_name: str
    ai_generated: bool
    steps: list[PlanStepView]
    phases: list[PhaseView]


class ProgressStepView(BaseModel):
    title: str
    status: str                      # done | current | failed | pending
    detail: str


class EventView(BaseModel):
    time: str
    agent: str
    action: str
    status: str
    detail: str
    duration_ms: float | None


class ApprovalView(BaseModel):
    checkpoint: str
    decision: str
    time: str
    note: str
    choice: str


class StageView(BaseModel):
    agent: str
    title: str
    status: str
    detail: str


class FileInfo(BaseModel):
    path: str
    size: int
    sha256: str
    author: str
    covers: list[int]
    language: str | None


class WorkspaceInfo(BaseModel):
    display_path: str                # ~/AETHER-projects/<run id>/workspace (never an absolute path)
    exists: bool


class Actions(BaseModel):
    """What the user could do at Checkpoint A. Eligibility only: the decision endpoint arrives in Phase 3."""

    can_approve: bool
    can_reject: bool
    can_override: bool
    disabled_reason: str | None      # why approval is unavailable, whenever can_approve is false


# ------------------------------------------------------------------ run view

class RunView(BaseModel):
    """One run: from its stored context (source "run") or from a history snapshot (source "snapshot")."""

    source: RunSource
    run_id: str | None
    entry_id: str | None             # the history entry, for snapshots
    context_state: ContextState      # whether the stored run could be loaded
    status: str | None               # the run's status; null when no stored run is available
    requirement: str
    started_at: str
    duration_ms: float | None        # only known from a history snapshot
    ok: bool                         # a plan exists
    error: str | None                # planning failed (for example an empty requirement)
    error_details: str | None
    developer_error: str | None      # the Developer's failure detail, if it failed
    brief: Brief | None
    plan: PlanView | None
    ai_generated: bool
    fallback_reason: str | None      # why the local AI planner was replaced by the rule-based one
    project_type: str | None
    template_title: str | None
    progress: list[ProgressStepView]
    events: list[EventView] | None   # null when no stored run is available
    approvals: list[ApprovalView]
    notes: dict[str, str]
    files: list[FileInfo]
    workspace: WorkspaceInfo | None
    actions: Actions
    warnings: list[str]              # things the loader noticed but tolerated, e.g. unknown fields
    stages: list[StageView] | None   # a snapshot's stages, shown when there is no stored run
    manager_log: list[str] | None    # only from a history snapshot


# ------------------------------------------------------------------ history

class HistoryEntry(BaseModel):
    entry_id: str
    run_id: str | None
    requirement: str
    started_at: str
    planner_name: str
    ai_generated: bool
    fallback_reason: str | None
    step_count: int
    features: list[FeatureRef]
    context_state: ContextState
    run_status: str | None
    file_count: int | None


class HistoryResponse(BaseModel):
    file_state: HistoryFileState
    entries: list[HistoryEntry]
    problems: list[str]              # entries that were skipped (damaged, or saved by a newer AETHER)
    max_entries: int


# ------------------------------------------------------------------ checkpoint preview

class Badge(BaseModel):
    label: str
    tone: Tone


class TemplateInfo(BaseModel):
    key: str
    title: str
    kind: str
    description: str


class FitInfo(BaseModel):
    status: str                      # selected | overridden | ambiguous | poor_fit | no_match
    reason: str
    badge: Badge
    evidence: list[str]
    hints: list[str]
    supported: list[FeatureRef]
    unsupported: list[FeatureRef]


class PlannedFile(BaseModel):
    path: str
    covers: list[int]


class OverridePreview(BaseModel):
    key: str
    title: str
    consequences: list[str]


class CheckpointPreview(BaseModel):
    """A preview only: reading it never approves, rejects, resumes or changes a run."""

    run_id: str
    available: bool                  # false when the run has no brief or plan
    reason: str | None
    status: str
    awaiting_approval: bool
    template: TemplateInfo | None
    fit: FitInfo | None
    planned_files: list[PlannedFile]
    uncovered_steps: list[int]
    candidates: list[TemplateInfo]
    override: OverridePreview | None
    actions: Actions


# ------------------------------------------------------------------ files

class FileListResponse(BaseModel):
    run_id: str
    workspace: WorkspaceInfo | None
    files: list[FileInfo]


# ------------------------------------------------------------------ jobs (task 014.2: read-only status of a job)

JobKind = Literal["plan", "approve"]
JobState = Literal["queued", "running", "succeeded", "failed", "interrupted"]   # "queued" is reserved, never produced
JobStage = Literal["understanding", "planning", "awaiting_approval", "generating", "finished"]


class LastEvent(BaseModel):
    agent: str
    action: str
    status: str


class JobProgress(BaseModel):
    stage: JobStage
    steps: list[ProgressStepView]    # Requirement, Plan, Approval, Generated files
    event_count: int
    last_event: LastEvent | None


class JobResult(BaseModel):
    """What a SUCCEEDED job reports. `succeeded` means the operation finished; read `run_status` for the project."""

    run_status: str | None
    ok: bool                         # a plan exists
    ai_generated: bool
    fallback_reason: str | None
    files_count: int | None
    warnings: list[str]              # for example "history_not_saved"


class JobError(BaseModel):
    """Why a job FAILED or was INTERRUPTED: a code and a fixed message, never an exception text, path or traceback."""

    code: str                        # storage_unavailable, internal_error, worker_lost, server_shutdown, ...
    message: str


class JobView(BaseModel):
    """One job. running: result and error are null. succeeded: result set. failed: error set.
    interrupted: error.code is "server_shutdown" and result is null."""

    job_id: str
    kind: JobKind
    state: JobState
    run_id: str | None               # null until a planning job has saved its run
    instance_id: str                 # random per server process: jobs of an earlier instance do not exist here
    created_at: str                  # the core's naive local ISO-8601 timestamps
    started_at: str | None
    finished_at: str | None
    duration_ms: float | None
    progress: JobProgress
    result: JobResult | None
    error: JobError | None


class FileContentResponse(BaseModel):
    path: str
    language: str | None
    size: int                        # the recorded size
    sha256: str                      # the recorded checksum
    current_sha256: str | None
    changed: bool                    # the file differs from what the Developer wrote
    missing: bool                    # the file is no longer on disk
    content: str | None
