import threading
import time
from typing import Callable

from fastapi import APIRouter, Depends, Request

from aether.config import planner_setting
from aether.ollama_planner import DEFAULT_MODEL, ollama_status
from aether_api import schemas
from aether_api.deps import get_repository
from aether_api.presenters import (
    EXAMPLES, PLANNER_LABELS, REQUIREMENT_MAX_CHARS, RUNS_FOLDER_MESSAGE, display_path, scrub,
)
from aether_api.repository import RunRepository
from aether_api.routers import ERRORS
from aether_ui import history

router = APIRouter(tags=["config"])


class StatusCache:
    """Remembers the Ollama check for a few seconds, as the Streamlit app does (a check can take 2 seconds)."""

    def __init__(self, ttl: float = 15.0, clock: Callable[[], float] = time.monotonic):
        self.ttl, self.clock = ttl, clock
        self._lock = threading.Lock()
        self._value: tuple[bool, str] | None = None
        self._at = 0.0

    def get(self, load: Callable[[], tuple[bool, str]]) -> tuple[bool, str]:
        with self._lock:
            if self._value is None or self.clock() - self._at >= self.ttl:
                self._value, self._at = load(), self.clock()
            return self._value


@router.get("/config", response_model=schemas.ConfigResponse, responses=ERRORS, operation_id="getConfig")
def get_config(repository: RunRepository = Depends(get_repository)) -> schemas.ConfigResponse:
    """Planner choices, limits, examples and where data is kept (short ~/ paths only)."""
    warnings = []
    try:
        default = planner_setting()
    except ValueError as error:
        default = "rule"
        warnings.append(schemas.ConfigWarning(code="invalid_planner_setting",
                                              message=f"{scrub(str(error))} The rule-based planner is used."))
    folder = repository.runs_folder()
    if not folder.valid:
        warnings.append(schemas.ConfigWarning(code="runs_folder_invalid", message=RUNS_FOLDER_MESSAGE))
    return schemas.ConfigResponse(
        planners=[schemas.PlannerOption(key=key, label=label) for key, label in PLANNER_LABELS.items()],
        default_planner=default, model=DEFAULT_MODEL,
        limits=schemas.Limits(requirement_max_chars=REQUIREMENT_MAX_CHARS, history_max_entries=history.MAX_ENTRIES),
        examples=[schemas.ExampleRequirement(label=label, text=text) for label, text in EXAMPLES],
        locations=schemas.Locations(runs=display_path(folder.root) if folder.valid else None,
                                   history=display_path(history.history_path()), runs_folder_valid=folder.valid),
        warnings=warnings)


@router.get("/planners/ollama/status", response_model=schemas.OllamaStatusResponse, responses=ERRORS,
            operation_id="getOllamaStatus")
def get_ollama_status(request: Request) -> schemas.OllamaStatusResponse:
    """Is the local Ollama server running with the approved model? Only ever contacts this computer."""
    ready, message = request.app.state.ollama_cache.get(ollama_status)
    return schemas.OllamaStatusResponse(ready=ready, message=message, model=DEFAULT_MODEL)
