"""In-memory background job registry (Phase 3, task 014.2).

A job is a view of one operation in progress (later: planning a run, running the Developer after an approval). The
run on disk is the durable truth; jobs are deliberately NOT persisted, so after a restart an old job id is simply
unknown (see `instance_id`). This module knows nothing about runs, files, FastAPI or AI: it only decides who may
start, tracks state, and guarantees the rules below. Standard library only, and it never touches the filesystem.

Rules (all tested):
  * Admission is ONE atomic reservation under the registry mutex: shutting down, idempotent replay, one active job per
    run, `max_jobs`, `max_ollama_jobs`. Counters live only inside the mutex, so simultaneous requests can never exceed
    a limit. The reservation is released exactly once: by the job's terminal transition, or on a failed thread start.
  * States are running, succeeded, failed, interrupted (queued is reserved and never produced: there is no queue).
    Terminal states are IMMUTABLE (compare-and-set under the mutex): a worker finishing after the job was marked
    interrupted cannot change it, so an interrupted job can never look successful and never carries a result.
  * The mutex is a LEAF: it is never held while the operation runs, while a thread starts, while logging, or while
    waiting (shutdown waits on a Condition, which releases it). There is no filesystem or AI call in this module.
  * Workers are daemon threads, so process exit is never blocked by an unfinished (for example AI) call.
  * Finished jobs are purged lazily (age `finished_ttl`, count `max_finished`); running jobs are never purged.
  * `instance_id` is random per registry (per server process). Job ids are random 128-bit values, so an id from an
    earlier instance is simply not found in a new one.
"""

import collections
import logging
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

logger = logging.getLogger("aether_api.jobs")

QUEUED, RUNNING, SUCCEEDED, FAILED, INTERRUPTED = "queued", "running", "succeeded", "failed", "interrupted"
TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, INTERRUPTED})
JOB_KINDS = ("plan", "approve")
STAGES = ("understanding", "planning", "awaiting_approval", "generating", "finished")
STEP_TITLES = ("Requirement", "Plan", "Approval", "Generated files")
INITIAL_STAGE = {"plan": "understanding", "approve": "generating"}

DEFAULT_MAX_JOBS = 2
DEFAULT_MAX_OLLAMA_JOBS = 1
DEFAULT_FINISHED_TTL = 3600.0   # seconds a finished job stays readable
DEFAULT_MAX_FINISHED = 200
DEFAULT_SHUTDOWN_GRACE = 5.0    # seconds to wait for running jobs at shutdown

# Fixed, safe texts (never an exception message, path or traceback).
INTERNAL_ERROR_MESSAGE = "The operation failed unexpectedly."
WORKER_LOST_MESSAGE = "The background worker stopped before the operation finished."
SHUTDOWN_MESSAGE = "The server stopped before the operation finished."


def _wall_now() -> str:
    """The core's timestamp convention: naive local ISO-8601 with seconds."""
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------ plain data (what callers see)

@dataclass(frozen=True)
class ProgressStep:
    title: str
    status: str = "pending"     # done | current | failed | pending
    detail: str = ""


@dataclass(frozen=True)
class LastEvent:
    agent: str
    action: str
    status: str


@dataclass(frozen=True)
class JobProgress:
    stage: str
    steps: tuple = ()
    event_count: int = 0
    last_event: Optional[LastEvent] = None


@dataclass(frozen=True)
class JobOutcome:
    """What a finished operation reports (the run itself may still be failed or cancelled: that is run state)."""

    run_status: Optional[str] = None
    ok: bool = False
    ai_generated: bool = False
    fallback_reason: Optional[str] = None
    files_count: Optional[int] = None
    warnings: tuple = ()


@dataclass(frozen=True)
class JobProblem:
    code: str
    message: str


@dataclass(frozen=True)
class JobSnapshot:
    """An immutable copy of a job at one moment. Safe to read without any lock."""

    job_id: str
    kind: str
    state: str
    run_id: Optional[str]
    instance_id: str
    created_at: str
    started_at: Optional[str]
    finished_at: Optional[str]
    duration_ms: Optional[float]
    progress: JobProgress
    result: Optional[JobOutcome]
    error: Optional[JobProblem]


@dataclass(frozen=True)
class Admission:
    job: JobSnapshot
    replayed: bool   # True when an Idempotency-Key matched an existing job (nothing new was started)


# ------------------------------------------------------------------ errors

class JobFailure(Exception):
    """Raised by an operation to fail its job with a specific, safe code and message."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class AdmissionError(Exception):
    """A job could not be admitted. `code` matches the API error code the HTTP layer will use."""

    code = "admission_refused"
    details: Optional[dict] = None


class JobsShuttingDown(AdmissionError):
    code = "shutting_down"


class TooManyJobs(AdmissionError):
    code = "too_many_jobs"


class PlannerBusy(AdmissionError):
    code = "planner_busy"


class RunBusy(AdmissionError):
    code = "run_busy"

    def __init__(self, job_id: Optional[str] = None):
        super().__init__("This run already has an active operation.")
        self.details = {"job_id": job_id} if job_id else None


class IdempotencyKeyReused(AdmissionError):
    code = "idempotency_key_reused"


class JobStartFailed(AdmissionError):
    code = "job_start_failed"


class JobNotFound(LookupError):
    """No job with this id exists in THIS server instance (unknown, purged, or from before a restart)."""

    def __init__(self, instance_id: str):
        super().__init__("There is no job with this id.")
        self.instance_id = instance_id


# ------------------------------------------------------------------ the registry

class _Job:
    __slots__ = ("job_id", "kind", "run_id", "state", "created_at", "started_at", "finished_at", "duration_ms",
                 "t0", "progress", "result", "error", "thread", "uses_ollama", "key_ref")

    def __init__(self, job_id: str, kind: str, run_id: Optional[str], created_at: str, t0: float, uses_ollama: bool):
        self.job_id, self.kind, self.run_id = job_id, kind, run_id
        self.state = RUNNING
        self.created_at, self.started_at, self.finished_at, self.duration_ms = created_at, None, None, None
        self.t0 = t0
        self.progress = JobProgress(INITIAL_STAGE[kind], tuple(ProgressStep(title) for title in STEP_TITLES))
        self.result: Optional[JobOutcome] = None
        self.error: Optional[JobProblem] = None
        self.thread: Optional[threading.Thread] = None
        self.uses_ollama = uses_ollama
        self.key_ref: Optional[tuple] = None


class JobContext:
    """What a running operation may do to its own job. Every call is ignored once the job is no longer running."""

    def __init__(self, registry: "JobRegistry", job: _Job):
        self._registry, self._job = registry, job

    @property
    def job_id(self) -> str:
        return self._job.job_id

    def set_run_id(self, run_id: str) -> None:
        """Say which run this job works on (a planning job learns its run id after the first save)."""
        self._registry._set_run_id(self._job, run_id)

    def set_progress(self, stage: str, steps=None, event_count: Optional[int] = None,
                     last_event: Optional[LastEvent] = None) -> None:
        self._registry._set_progress(self._job, stage, steps, event_count, last_event)


class JobRegistry:
    def __init__(self, *, max_jobs: int = DEFAULT_MAX_JOBS, max_ollama_jobs: int = DEFAULT_MAX_OLLAMA_JOBS,
                 finished_ttl: float = DEFAULT_FINISHED_TTL, max_finished: int = DEFAULT_MAX_FINISHED,
                 shutdown_grace: float = DEFAULT_SHUTDOWN_GRACE, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], str] = _wall_now, instance_id: Optional[str] = None):
        if max_jobs < 1 or max_ollama_jobs < 1 or max_finished < 0 or finished_ttl < 0 or shutdown_grace < 0:
            raise ValueError("Job limits must be positive (retention and grace may be zero).")
        self.max_jobs, self.max_ollama_jobs = max_jobs, max_ollama_jobs
        self.finished_ttl, self.max_finished, self.shutdown_grace = finished_ttl, max_finished, shutdown_grace
        self.instance_id = instance_id or secrets.token_hex(6)   # 12 hex characters, random per server process
        self._clock, self._wall = clock, wall                    # the injected clock is only for retention
        self._lock = threading.Lock()                            # THE mutex: a leaf, never held across I/O or work
        self._cond = threading.Condition(self._lock)
        self._jobs: dict[str, _Job] = {}
        self._finished: "collections.OrderedDict[str, float]" = collections.OrderedDict()  # job id -> finished (clock)
        self._keys: dict[tuple, tuple] = {}                      # (kind, run id, key) -> (job id, request hash)
        self._active_by_run: dict[str, str] = {}                 # run id -> job id of its active job
        self._active = 0
        self._active_ollama = 0
        self._shutting_down = False

    # -------------------------------------------------------------- admission

    def submit(self, kind: str, work: Callable[[JobContext], Optional[JobOutcome]], *, run_id: Optional[str] = None,
               uses_ollama: bool = False, idempotency_key: Optional[str] = None,
               request_hash: Optional[str] = None) -> Admission:
        """Admit a job and start it on a daemon thread, or raise an AdmissionError. Never blocks on the operation.

        Order of the checks (the first that applies wins): shutting down, idempotent replay, run already busy, too many
        jobs, AI planner busy. An idempotent replay does not use a slot, so it works even at capacity."""
        if kind not in JOB_KINDS:
            raise ValueError(f"Unknown job kind {kind!r}.")
        if idempotency_key is not None and request_hash is None:
            raise ValueError("An idempotency key needs a request hash.")
        with self._lock:
            self._purge_locked()
            if self._shutting_down:
                raise JobsShuttingDown("The server is shutting down.")
            if idempotency_key is not None:
                known = self._keys.get((kind, run_id, idempotency_key))
                if known is not None:
                    job_id, stored_hash = known
                    if stored_hash != request_hash:
                        raise IdempotencyKeyReused("This Idempotency-Key was already used for a different request.")
                    return Admission(self._snapshot_locked(self._jobs[job_id]), True)
            if run_id is not None and run_id in self._active_by_run:
                raise RunBusy(self._active_by_run[run_id])
            if self._active >= self.max_jobs:
                raise TooManyJobs("Too many jobs are running.")
            if uses_ollama and self._active_ollama >= self.max_ollama_jobs:
                raise PlannerBusy("The local AI planner is already busy.")
            job = self._create_locked(kind, run_id, uses_ollama, idempotency_key, request_hash)
        # The mutex is released here: starting a thread is never done while holding it.
        try:
            worker = threading.Thread(target=self._run, args=(job, work), name=f"aether-job-{job.job_id[:8]}", daemon=True)
            worker.start()
        except BaseException as error:
            with self._lock:
                self._unreserve_locked(job)
            raise JobStartFailed("The background worker could not be started.") from error
        with self._lock:
            job.thread = worker
            return Admission(self._snapshot_locked(job), False)

    def _create_locked(self, kind, run_id, uses_ollama, idempotency_key, request_hash) -> _Job:
        job_id = uuid.uuid4().hex
        while job_id in self._jobs:  # a collision is astronomically unlikely, but an id must never be reused
            job_id = uuid.uuid4().hex
        job = _Job(job_id, kind, run_id, self._wall(), self._clock(), uses_ollama)
        self._jobs[job_id] = job
        self._active += 1
        if uses_ollama:
            self._active_ollama += 1
        if run_id is not None:
            self._active_by_run[run_id] = job_id
        if idempotency_key is not None:
            job.key_ref = (kind, run_id, idempotency_key)
            self._keys[job.key_ref] = (job_id, request_hash)
        return job

    def _unreserve_locked(self, job: _Job) -> None:
        """Undo a reservation whose worker never started: no job, no counters, no key, no run claim is left."""
        if self._jobs.pop(job.job_id, None) is None:
            return
        job.state = FAILED  # defensive: nothing can observe it now
        self._active -= 1
        if job.uses_ollama:
            self._active_ollama -= 1
        if job.run_id is not None and self._active_by_run.get(job.run_id) == job.job_id:
            del self._active_by_run[job.run_id]
        if job.key_ref is not None:
            self._keys.pop(job.key_ref, None)

    # -------------------------------------------------------------- the worker

    def _run(self, job: _Job, work: Callable[[JobContext], Optional[JobOutcome]]) -> None:
        with self._lock:
            if job.state == RUNNING:
                job.started_at = self._wall()
        try:
            outcome = work(JobContext(self, job))
        except JobFailure as failure:
            state, result, error = FAILED, None, JobProblem(failure.code, failure.message)
        except BaseException:  # including SystemExit and KeyboardInterrupt raised inside the worker: it must END
            logger.error("Job %s failed unexpectedly.", job.job_id, exc_info=True)  # traceback to the server log only
            state, result, error = FAILED, None, JobProblem("internal_error", INTERNAL_ERROR_MESSAGE)
        else:
            state, result, error = SUCCEEDED, outcome if outcome is not None else JobOutcome(), None
        if not self._finish(job, state, result, error):
            logger.warning("Job %s finished after it was already %s; the late result was ignored.", job.job_id, job.state)

    def _finish(self, job: _Job, state: str, result: Optional[JobOutcome], error: Optional[JobProblem]) -> bool:
        with self._lock:
            return self._finish_locked(job, state, result, error)

    def _finish_locked(self, job: _Job, state: str, result: Optional[JobOutcome], error: Optional[JobProblem]) -> bool:
        """The ONLY way a job leaves `running`. Compare-and-set: a job that is already terminal is left alone."""
        if job.state != RUNNING or job.job_id not in self._jobs:
            return False
        job.state, job.result, job.error = state, result, error
        job.finished_at = self._wall()
        job.duration_ms = round((self._clock() - job.t0) * 1000, 1)
        job.progress = JobProgress("finished", job.progress.steps, job.progress.event_count, job.progress.last_event)
        self._active -= 1
        if job.uses_ollama:
            self._active_ollama -= 1
        if job.run_id is not None and self._active_by_run.get(job.run_id) == job.job_id:
            del self._active_by_run[job.run_id]
        self._finished[job.job_id] = self._clock()
        self._purge_locked()
        self._cond.notify_all()
        return True

    # -------------------------------------------------------------- updates from a running operation

    def _set_run_id(self, job: _Job, run_id: str) -> None:
        with self._lock:
            if job.state != RUNNING:
                return
            if job.run_id is not None and job.run_id != run_id:
                raise ValueError("A job's run id cannot change.")
            owner = self._active_by_run.get(run_id)
            if owner is not None and owner != job.job_id:
                raise ValueError("Another active job already owns this run.")
            job.run_id = run_id
            self._active_by_run[run_id] = job.job_id

    def _set_progress(self, job: _Job, stage: str, steps, event_count, last_event) -> None:
        if stage not in STAGES:
            raise ValueError(f"Unknown progress stage {stage!r}.")
        with self._lock:
            if job.state != RUNNING:
                return
            old = job.progress
            job.progress = JobProgress(
                stage, old.steps if steps is None else tuple(steps),
                old.event_count if event_count is None else event_count,
                old.last_event if last_event is None else last_event)

    # -------------------------------------------------------------- reading

    def get(self, job_id: str) -> Optional[JobSnapshot]:
        """A snapshot of the job, or None if this instance has no such job. Also fails a job whose worker thread died
        without finishing it (`worker_lost`), so a job can never stay `running` forever."""
        lost = False
        with self._lock:
            self._purge_locked()
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.state == RUNNING and job.thread is not None and not job.thread.is_alive():
                lost = self._finish_locked(job, FAILED, None, JobProblem("worker_lost", WORKER_LOST_MESSAGE))
            snapshot = self._snapshot_locked(job)
        if lost:
            logger.error("Job %s: its worker thread ended without finishing the job.", job_id)
        return snapshot

    def stats(self) -> dict:
        with self._lock:
            self._purge_locked()
            return {"active": self._active, "active_ollama": self._active_ollama, "jobs": len(self._jobs),
                    "finished": len(self._finished), "idempotency_keys": len(self._keys),
                    "runs_claimed": len(self._active_by_run), "shutting_down": self._shutting_down}

    @property
    def shutting_down(self) -> bool:
        with self._lock:
            return self._shutting_down

    def check_invariants(self) -> None:
        """Raise AssertionError if the counters disagree with the jobs (used by tests and diagnostics)."""
        with self._lock:
            running = [j for j in self._jobs.values() if j.state == RUNNING]
            assert self._active == len(running), (self._active, len(running))
            assert self._active_ollama == sum(1 for j in running if j.uses_ollama), self._active_ollama
            assert self._active >= 0 and self._active_ollama >= 0
            assert set(self._active_by_run.values()) <= {j.job_id for j in running}
            assert all(j.state in TERMINAL_STATES for j in self._jobs.values() if j.job_id in self._finished)
            assert set(self._finished) == {j.job_id for j in self._jobs.values() if j.state in TERMINAL_STATES}
            assert all(job_id in self._jobs for job_id, _ in self._keys.values())

    # -------------------------------------------------------------- retention

    def _purge_locked(self) -> None:
        """Drop finished jobs that are too old or beyond `max_finished` (oldest first). Running jobs are never dropped."""
        now = self._clock()
        while self._finished:
            job_id, finished_at = next(iter(self._finished.items()))
            if now - finished_at >= self.finished_ttl or len(self._finished) > self.max_finished:
                self._drop_locked(job_id)
            else:
                break

    def _drop_locked(self, job_id: str) -> None:
        self._finished.pop(job_id, None)
        job = self._jobs.pop(job_id, None)
        if job is not None and job.key_ref is not None:
            self._keys.pop(job.key_ref, None)

    # -------------------------------------------------------------- shutdown

    def shutdown(self, grace: Optional[float] = None) -> list:
        """Stop admitting jobs, give running jobs `grace` seconds, then mark what is left `interrupted`.

        An interrupted job has `result = None` and `error.code = "server_shutdown"`, and stays that way even if its
        thread later finishes. Waiting happens on a Condition, which releases the mutex, so reads and refusals keep
        working meanwhile. Safe to call twice. Returns the ids of the jobs it interrupted."""
        grace = self.shutdown_grace if grace is None else grace
        deadline = time.monotonic() + grace   # real time: the injected clock is only for retention
        interrupted = []
        with self._cond:
            self._shutting_down = True
            while self._active > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=remaining)
            for job in list(self._jobs.values()):
                if job.state == RUNNING and self._finish_locked(
                        job, INTERRUPTED, None, JobProblem("server_shutdown", SHUTDOWN_MESSAGE)):
                    interrupted.append(job.job_id)
        for job_id in interrupted:
            logger.warning("Job %s was interrupted by server shutdown.", job_id)
        return interrupted

    # -------------------------------------------------------------- snapshots

    def _snapshot_locked(self, job: _Job) -> JobSnapshot:
        return JobSnapshot(
            job_id=job.job_id, kind=job.kind, state=job.state, run_id=job.run_id, instance_id=self.instance_id,
            created_at=job.created_at, started_at=job.started_at, finished_at=job.finished_at,
            duration_ms=job.duration_ms, progress=job.progress, result=job.result, error=job.error)
