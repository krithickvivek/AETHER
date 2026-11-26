from fastapi import Request

from aether_api.repository import RunRepository


def get_repository(request: Request) -> RunRepository:
    """The repository chosen when the app was created (the file-based one unless a test supplies another)."""
    return request.app.state.repository
