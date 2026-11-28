from fastapi import Request

from aether_api.jobs import JobRegistry
from aether_api.repository import RunRepository


def get_repository(request: Request) -> RunRepository:
    """The repository chosen when the app was created (the file-based one unless a test supplies another)."""
    return request.app.state.repository


def get_jobs(request: Request) -> JobRegistry:
    """This server instance's job registry (created with the app; jobs are not shared between instances)."""
    return request.app.state.jobs
