from fastapi import APIRouter, Depends, Path, Query

from aether_api import schemas
from aether_api.deps import get_repository
from aether_api.presenters import checkpoint_preview, display_path, run_view, synthesize_result
from aether_api.repository import RunRepository
from aether_api.routers import ERRORS

router = APIRouter(tags=["runs"])
RunId = Path(min_length=1, max_length=64, description="The run id (letters and digits).")


@router.get("/runs/{run_id}", response_model=schemas.RunView, responses=ERRORS, operation_id="getRun")
def get_run(run_id: str = RunId, repository: RunRepository = Depends(get_repository)) -> schemas.RunView:
    """A stored run: status, plan, progress, activity, approvals, files and what Checkpoint A would allow.
    A failed or cancelled run is returned as run state (status), not as an error."""
    context = repository.load_run(run_id)  # 404/422/503 happen here, before anything else is touched
    snapshot = repository.snapshot_for_run(run_id)  # run time and Manager log live only in history
    folder = repository.runs_folder()
    workspace_display = display_path(folder.root / context.run_id / "workspace") if folder.valid else None
    return run_view(source="run", result=synthesize_result(context, snapshot), context=context, context_state="ok",
                    folder_valid=folder.valid, snapshot=snapshot,
                    workspace_exists=repository.workspace_exists(context.run_id), workspace_display=workspace_display)


@router.get("/runs/{run_id}/checkpoint", response_model=schemas.CheckpointPreview, responses=ERRORS,
            operation_id="getCheckpointPreview")
def get_checkpoint(run_id: str = RunId,
                   override: str | None = Query(None, max_length=64,
                                                description="Preview the consequences of building this template anyway."),
                   repository: RunRepository = Depends(get_repository)) -> schemas.CheckpointPreview:
    """Checkpoint A as it would be shown. A preview only: it never approves, rejects, resumes or changes a run."""
    context = repository.load_run(run_id)
    return checkpoint_preview(context, repository.runs_folder().valid, override)
