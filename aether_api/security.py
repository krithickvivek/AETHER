"""Localhost-only protection for the API: a Host allow-list and an Origin check.

Why: the API listens on 127.0.0.1, but any web page the user opens can still try to reach it.
  * A wrong Host header means a DNS-rebinding attempt or a misdirected request: refused.
  * A browser sends Origin on cross-origin requests, so an Origin that is not ours is refused on every method.
  * Every method other than GET must carry an allowed Origin (even without a body); and in this read-only phase
    no other method is supported at all, so an allowed one is then answered with 405.
There is no CORS: the browser never reads this API from another origin. In development the Vite dev server
proxies /api (configured with changeOrigin: true in Phase 4), so its requests arrive with the API's own Host.
"""

from dataclasses import dataclass

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from aether_api import DEFAULT_PORT, DEV_PORT


@dataclass(frozen=True)
class SecuritySettings:
    port: int = DEFAULT_PORT
    dev: bool = False          # also accept the Vite development server (localhost:5173 only)
    dev_port: int = DEV_PORT

    def _ports(self) -> list[int]:
        return [self.port] + ([self.dev_port] if self.dev else [])

    @property
    def allowed_hosts(self) -> frozenset:
        return frozenset(f"{name}:{port}" for port in self._ports() for name in ("localhost", "127.0.0.1"))

    @property
    def allowed_origins(self) -> frozenset:
        return frozenset(f"http://{host}" for host in self.allowed_hosts)


def _refuse(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, "details": None}}, status_code=status,
                        headers=headers)


class LocalhostGuard:
    """Pure ASGI middleware, outermost layer: it answers before any route or database access."""

    def __init__(self, app, settings: SecuritySettings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":  # the API has no websockets
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        refusal = self.check(scope["method"], headers.get("host"), headers.get("origin"))
        if refusal is not None:
            await refusal(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def check(self, method: str, host: str | None, origin: str | None) -> JSONResponse | None:
        if host is None or host.lower() not in self.settings.allowed_hosts:
            return _refuse(403, "host_not_allowed", "This API only answers requests addressed to localhost.")
        if origin is not None and origin.lower() not in self.settings.allowed_origins:
            return _refuse(403, "origin_not_allowed", "Requests from this origin are not allowed.")
        if method != "GET":
            if origin is None:
                return _refuse(403, "origin_required", "Requests other than GET must come from an allowed origin.")
            return _refuse(405, "method_not_allowed", "This API is read-only: only GET is supported.",
                           {"Allow": "GET"})
        return None
