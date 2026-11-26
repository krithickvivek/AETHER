"""The application factory. `create_app()` builds a fresh, self-contained app (tests make many of them)."""

from fastapi import FastAPI

from aether_api import API_PREFIX, API_VERSION
from aether_api.errors import install_error_handlers
from aether_api.repository import FileRunRepository, RunRepository
from aether_api.routers import config, files, health, history, runs
from aether_api.routers.config import StatusCache
from aether_api.security import LocalhostGuard, SecuritySettings


def create_app(settings: SecuritySettings | None = None, repository: RunRepository | None = None) -> FastAPI:
    settings = settings or SecuritySettings()
    app = FastAPI(
        title="AETHER API", version=API_VERSION, openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=None, redoc_url=None,  # the bundled docs pages load scripts from a CDN; this API stays offline
        description="Read-only API over AETHER's saved runs, history and generated files (Phase 2).",
    )
    app.state.repository = repository or FileRunRepository()
    app.state.ollama_cache = StatusCache()
    install_error_handlers(app)
    for module in (health, config, history, runs, files):
        app.include_router(module.router, prefix=API_PREFIX)
    app.add_middleware(LocalhostGuard, settings=settings)  # added last, so it is the outermost layer
    return app
