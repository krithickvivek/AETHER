from fastapi import APIRouter, Depends, Path, Response

from aether_api import schemas
from aether_api.deps import get_jobs
from aether_api.jobs import JobNotFound, JobRegistry, JobSnapshot
from aether_api.presenters import scrub
from aether_api.routers import ERRORS

router = APIRouter(tags=["jobs"])


def job_view(snapshot: JobSnapshot) -> schemas.JobView:
    """The response model for a job snapshot. Messages are fixed texts; they are scrubbed again as a safety net."""
    progress = snapshot.progress
    result, error = snapshot.result, snapshot.error
    return schemas.JobView(
        job_id=snapshot.job_id, kind=snapshot.kind, state=snapshot.state, run_id=snapshot.run_id,
        instance_id=snapshot.instance_id, created_at=snapshot.created_at, started_at=snapshot.started_at,
        finished_at=snapshot.finished_at, duration_ms=snapshot.duration_ms,
        progress=schemas.JobProgress(
            stage=progress.stage,
            steps=[schemas.ProgressStepView(title=s.title, status=s.status, detail=scrub(s.detail) or "")
                   for s in progress.steps],
            event_count=progress.event_count,
            last_event=None if progress.last_event is None else schemas.LastEvent(
                agent=progress.last_event.agent, action=progress.last_event.action, status=progress.last_event.status)),
        result=None if result is None else schemas.JobResult(
            run_status=result.run_status, ok=result.ok, ai_generated=result.ai_generated,
            fallback_reason=scrub(result.fallback_reason), files_count=result.files_count,
            warnings=list(result.warnings)),
        error=None if error is None else schemas.JobError(code=error.code, message=scrub(error.message) or ""))


@router.get("/jobs/{job_id}", response_model=schemas.JobView, responses=ERRORS, operation_id="getJob")
def get_job(response: Response, job_id: str = Path(min_length=1, max_length=64, description="The job id."),
            jobs: JobRegistry = Depends(get_jobs)) -> schemas.JobView:
    """The status of a background job. Jobs live in memory only: after a server restart (or once purged) an old job
    id is `404 job_not_found`, and the answer names the CURRENT `instance_id`; read the run instead."""
    snapshot = jobs.get(job_id)
    if snapshot is None:
        raise JobNotFound(jobs.instance_id)
    response.headers["Cache-Control"] = "no-store"
    return job_view(snapshot)
