from fastapi import APIRouter

from aether.schema import SCHEMA_VERSION
from aether_api import API_VERSION
from aether_api.routers import ERRORS
from aether_api.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, responses=ERRORS, operation_id="getHealth")
def get_health() -> HealthResponse:
    """Always answers without touching the disk."""
    return HealthResponse(status="ok", api_version=API_VERSION, schema_version=SCHEMA_VERSION, read_only=True)
