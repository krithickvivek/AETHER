from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import Response

from aether_api import schemas
from aether_api.deps import get_repository
from aether_api.presenters import display_path, file_infos, language_of
from aether_api.repository import RunNotFound, RunRepository
from aether_api.routers import ERRORS
from aether_ui.workflow import plan_to_markdown

router = APIRouter(tags=["files"])
RunId = Path(min_length=1, max_length=64, description="The run id (letters and digits).")


@router.get("/runs/{run_id}/files", response_model=schemas.FileListResponse, responses=ERRORS,
            operation_id="listFiles")
def list_files(run_id: str = RunId, repository: RunRepository = Depends(get_repository)) -> schemas.FileListResponse:
    """The files the Developer recorded for this run. Read from the run's record, so no folder is opened or created."""
    context = repository.load_run(run_id)
    folder = repository.runs_folder()
    workspace = schemas.WorkspaceInfo(display_path=display_path(folder.root / context.run_id / "workspace"),
                                      exists=repository.workspace_exists(context.run_id)) if folder.valid else None
    return schemas.FileListResponse(run_id=context.run_id, workspace=workspace, files=file_infos(context))


@router.get("/runs/{run_id}/files/content", response_model=schemas.FileContentResponse, responses=ERRORS,
            operation_id="getFileContent")
def get_file_content(run_id: str = RunId,
                     path: str = Query(min_length=1, max_length=500, description="A project-relative file path."),
                     repository: RunRepository = Depends(get_repository)) -> schemas.FileContentResponse:
    """One generated file's text, with a flag saying whether it changed since the Developer wrote it.
    The run is loaded first; a Workspace is only built when the run has files and its folder already exists."""
    context = repository.load_run(run_id)
    content = repository.read_file(context, path)
    record = content.record
    changed = content.current_sha256 is not None and content.current_sha256 != record.sha256
    return schemas.FileContentResponse(
        path=record.path, language=language_of(record.path), size=record.size, sha256=record.sha256,
        current_sha256=content.current_sha256, changed=changed, missing=content.missing, content=content.text)


@router.get("/runs/{run_id}/plan.md", response_class=Response, responses={**ERRORS, 200: {"content": {"text/markdown": {}}}},
            operation_id="getPlanMarkdown")
def get_plan_markdown(run_id: str = RunId, repository: RunRepository = Depends(get_repository)) -> Response:
    """The plan as a Markdown document (the same text the Streamlit download gives)."""
    context = repository.load_run(run_id)
    if context.plan is None:
        raise RunNotFound("This run has no plan.")
    return Response(plan_to_markdown(context.plan), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="aether_plan.md"'})
