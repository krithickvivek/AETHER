from fastapi import APIRouter, Depends, Path

from aether_api import schemas
from aether_api.deps import get_repository
from aether_api.presenters import display_path, feature_refs, run_view, scrub
from aether_api.repository import RunRepository
from aether_api.routers import ERRORS
from aether_ui import history
from aether_ui.workflow import RunResult

router = APIRouter(tags=["history"])


def _summary(repository: RunRepository, entry: dict) -> schemas.HistoryEntry:
    result: RunResult = history.entry_to_result(entry)
    state, context = repository.context_state(result.run_id)
    return schemas.HistoryEntry(
        entry_id=entry["id"], run_id=result.run_id, requirement=result.requirement, started_at=result.started_at,
        planner_name=result.plan.planner_name if result.plan else "", ai_generated=result.ai_generated,
        fallback_reason=scrub(result.fallback_reason), step_count=len(result.plan.steps) if result.plan else 0,
        features=feature_refs(result.brief.features if result.brief else []), context_state=state,
        run_status=context.status if context else None, file_count=len(context.files) if context else None)


@router.get("/history", response_model=schemas.HistoryResponse, responses=ERRORS, operation_id="listHistory")
def list_history(repository: RunRepository = Depends(get_repository)) -> schemas.HistoryResponse:
    """Saved plans, newest first. `file_state` and `problems` say when the history file is missing, damaged,
    unreadable, or has entries that were skipped; a damaged history is never shown as an empty healthy one."""
    read = repository.read_history()
    return schemas.HistoryResponse(
        file_state=read.file_state, entries=[_summary(repository, entry) for entry in read.entries],
        problems=[scrub(p) or "" for p in read.problems], max_entries=history.MAX_ENTRIES)


@router.get("/history/{entry_id}", response_model=schemas.RunView, responses=ERRORS, operation_id="getHistoryEntry")
def get_history_entry(entry_id: str = Path(min_length=1, max_length=64),
                      repository: RunRepository = Depends(get_repository)) -> schemas.RunView:
    """A saved plan as a snapshot, with its stored run when that can still be loaded (as the History page opens it)."""
    _, result = repository.history_entry(entry_id)
    state, context = repository.context_state(result.run_id)
    folder = repository.runs_folder()
    exists = bool(context) and repository.workspace_exists(context.run_id)
    workspace_display = display_path(folder.root / context.run_id / "workspace") if context and folder.valid else None
    return run_view(source="snapshot", result=result, context=context, context_state=state, folder_valid=folder.valid,
                    entry_id=entry_id, snapshot=result, workspace_exists=exists, workspace_display=workspace_display)
