"""The application factory. `create_app()` builds a fresh, self-contained app (tests make many of them)."""

import asyncio
import contextlib

from fastapi import FastAPI

from aether_api import API_PREFIX, API_VERSION
from aether_api.errors import install_error_handlers
from aether_api.jobs import JobRegistry
from aether_api.repository import FileRunRepository, RunRepository
from aether_api.routers import config, files, health, history, jobs, runs
from aether_api.routers.config import StatusCache
from aether_api.security import LocalhostGuard, SecuritySettings


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """On shutdown stop admitting jobs, give running ones a short grace period, and mark the rest interrupted.
    The wait runs in a worker thread so the event loop is never blocked."""
    yield
    await asyncio.to_thread(app.state.jobs.shutdown)


def create_app(settings: SecuritySettings | None = None, repository: RunRepository | None = None,
               job_registry: JobRegistry | None = None) -> FastAPI:
    settings = settings or SecuritySettings()
    app = FastAPI(
        title="AETHER API", version=API_VERSION, openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=None, redoc_url=None,  # the bundled docs pages load scripts from a CDN; this API stays offline
        description="API over AETHER's saved runs, history, generated files and background jobs. Phase 3 is adding "
                    "write operations step by step; so far every endpoint is a read.",
        lifespan=lifespan,
    )
    app.state.repository = repository or FileRunRepository()
    app.state.jobs = job_registry or JobRegistry()   # one registry per app = per server instance (its instance_id)
    app.state.ollama_cache = StatusCache()
    install_error_handlers(app)
    for module in (health, config, history, runs, files, jobs):
        app.include_router(module.router, prefix=API_PREFIX)
    app.add_middleware(LocalhostGuard, settings=settings)  # added last, so it is the outermost layer
    return app
