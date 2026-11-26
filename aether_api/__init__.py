"""AETHER web API: a read-only FastAPI layer over the existing AETHER core (Phase 2).

It reuses the `aether` package and the `aether_ui.workflow` / `aether_ui.history` helpers and adds no agent
logic. It never imports `app.py` or `aether_ui.theme` (those need Streamlit). In this phase every endpoint is a
read: nothing is written to a run, to history or to the generated files, and no workspace folder is created.
"""

API_VERSION = "v1"
API_PREFIX = "/api/v1"
BIND_HOST = "127.0.0.1"   # the API only ever listens on this computer
DEFAULT_PORT = 8765       # not 8000: the generated projects' servers use 127.0.0.1:8000
DEV_PORT = 5173           # the Vite development server (Phase 4)
