# AETHER

**AI-Enabled Team for Hierarchical Engineering and Reasoning**: a multi-agent software engineering assistant, built as a final-year project.

You describe the software you want. A **Manager** agent analyses the requirement, a **Planner** turns it into a phased development plan, you **review and approve** that plan at a checkpoint, and only then does a **Developer** agent write a tested project template into its own folder.

Everything runs on your computer. No API keys, paid services or cloud models are used.

## Problem and motivation

Turning an idea such as "a to-do website where users can log in" into working software starts with the same steps every time: understand what is asked, break it into phases, and decide what to build. Large language models can write code, but their output is hard to trust: it is not reproducible, it is rarely checked, and the person using it often cannot see what was decided or why.

AETHER explores a more controlled alternative. Work is split between small, specialised agents, every decision is recorded, a human approves the plan before anything is written, and the code that is produced comes from templates that were written and tested in advance rather than from a model.

## Objectives

- Show a multi-agent workflow (Manager, Planner, Developer) that turns a plain-language requirement into a phased plan.
- Keep a human in control through an explicit approval checkpoint before any file is created.
- Record what really happened (events, approvals, fallbacks, failures) instead of presenting an optimistic summary.
- Run entirely on one computer, with no accounts, API keys or paid services.
- Keep the core usable without a user interface, so the same logic serves the command line, the web interface and the web API.

## Architecture and workflow

```
Requirement ──► Manager ──► Planner ──► Plan ──► Checkpoint A (you approve or reject) ──► Developer ──► Project files
                  │            │                                                             │
                  └────────────┴──────────── every step is recorded in the run's activity log ┘
```

1. **Manager** cleans up the requirement, detects the features it mentions (login, data storage, web pages, API) from keywords, and builds a `TaskBrief`.
2. **Planner** turns the brief into a phased plan. The rule-based planner is the default; the optional local AI planner falls back to it automatically if anything goes wrong.
3. **Checkpoint A** pauses the run. Nothing is written until you approve; rejecting ends the run with no files.
4. **Developer** chooses a template conservatively, copies it into a separate run folder and records a SHA-256 checksum for each file.

Agents never change the shared context directly: they return results, and the Manager applies and records them. Each run is saved as `context.json` plus an `events.jsonl` activity log.

## Technologies used

| Area | Technology |
|---|---|
| Language | Python 3.10 or newer (developed with 3.11) |
| Core library, command line, generated projects | Python standard library only |
| Web interface | [Streamlit](https://streamlit.io) 1.57 or newer |
| Web API (read-only) | [FastAPI](https://fastapi.tiangolo.com) with Pydantic and Starlette, served by [Uvicorn](https://www.uvicorn.org); [httpx](https://www.python-httpx.org) for the API tests |
| Experimental local AI planner | [Ollama](https://ollama.com) with the `llama3.2:1b` model (optional) |
| Tests | Python `unittest`, Streamlit `AppTest` |
| Generated project data | SQLite, in the to-do and library templates only |

## Status at a glance

| Agent | Status | How it works today |
|---|---|---|
| Manager | Implemented | Rule-based. Cleans up the requirement, detects features by keyword (login, data storage, web pages, API), coordinates the other agents and records every step |
| Planner | Implemented | Rule-based by default. An optional, experimental planner uses a small local AI model (see below) |
| Developer | Implemented | Copies one of three tested project templates after you approve the plan. It never asks an AI model to write code, and it never runs the generated code |
| Reviewer | Not built yet | |
| Tester | Not built yet | |
| Documentation | Not built yet | |

## Features

- **Requirement → plan.** A phased plan (Requirements, Design, Setup, Implementation, Testing, Documentation). With the rule-based planner, each step is tagged with the rule that added it.
- **Checkpoint A (human approval).** The run pauses before any file is written. You see the template that would be used, how well it fits, and which file carries out which plan step, then approve or reject.
- **Conservative template selection.** A template is chosen only when the requirement names its project type (for example "to-do", "library" or "calculator") and the template supports at least as many of the requested features as it lacks. Ambiguous requirements and poor fits are not built automatically. You can still force a template through an explicit, acknowledged override, which is recorded in the activity log.
- **Generated project files.** Written to a separate folder outside AETHER, each with a SHA-256 checksum and the plan steps it covers. The file viewer warns if a file changed on disk afterwards.
- **Honest activity log.** Every status shown comes from what really happened in the run, including approvals, fallbacks and failures.
- **Run history.** The last 25 successful runs, saved on this computer and reopened without re-running anything.
- **Download the plan** as Markdown.
- **Web interface** (Streamlit) with dark and light themes, a workflow progress indicator and a layout that works on phones; plus a **command-line** planner.

### Project templates

| Template | What it is | Supports |
|---|---|---|
| Command-line calculator | Python CLI: add, subtract, multiply and divide, from arguments or an interactive prompt | Core only |
| To-do web app | Python standard-library web server with SQLite: register and log in (salted PBKDF2 password hashes), then add, complete and delete your own tasks | Login, data storage, web pages |
| Library management REST API | Python standard-library JSON API with SQLite: books, members and loans, with validation and availability checks | Data storage, API |

Templates use only Python's standard library, so a generated project runs without installing anything. Each one includes its own `unittest` suite and a README, and its servers listen on `127.0.0.1:8000` only.

## Requirements

- Python **3.10 or newer** (developed with 3.11)
- Web interface: Streamlit 1.57 or newer (installed from `requirements.txt`)
- Command line: nothing beyond Python

## Setup

```bash
git clone https://github.com/krithickvivek/AETHER.git AETHER
cd AETHER
python -m venv .venv
```

Activate the virtual environment (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate` on macOS and Linux), then:

```bash
python -m pip install -r requirements.txt
```

## Run the web app

```bash
python -m streamlit run app.py
```

Your browser opens at http://localhost:8501. Press `Ctrl+C` in the terminal to stop.

- **Workspace:** pick an example or describe your own software, then select **Generate plan**. Review the plan at **Checkpoint A**, then **Approve and generate files** or **Reject plan**. Generated files appear in the same place, with a file viewer. *Under the hood* shows the agent activity log, the Manager → Planner handoff and the exact `TaskBrief`.
- **History:** reopen or clear saved runs.
- **About:** what the prototype does and does not do.

The app follows your computer's light or dark setting. You can switch in the app menu (⋮ → Settings), and the choice is remembered in your browser.

## Run the command-line planner

```bash
python main.py "Build a to-do website where users can log in and save tasks"
```

Or run `python main.py` and type the requirement when asked. The command line prints the plan only; generating files needs the web app's approval checkpoint.

## Web API (read-only preview for the planned React interface)

AETHER also has a small web API (FastAPI) that shows saved runs, history and generated files to the planned React interface. It is **read-only**: it cannot create, approve, reject or change anything yet. The Streamlit app does not depend on it.

```bash
python -m pip install -r requirements-api.txt    # FastAPI, Uvicorn and httpx (httpx is used by the tests)
python -m aether_api                              # then open http://127.0.0.1:8765/api/v1/health
```

- It listens only on `127.0.0.1`, port 8765 (`--port` changes the port; there is no option to change the address). Requests whose `Host` or `Origin` is not localhost are refused, and there is no CORS.
- `python -m aether_api --dev` also accepts the Vite development server (`localhost:5173`), for the React interface.
- `python -m aether_api --dump-openapi` prints the OpenAPI document (used later to generate TypeScript types).
- It reads the same data as the Streamlit app (`AETHER_RUNS_DIR`, `AETHER_HISTORY_FILE`) and writes nothing.
- Endpoints, all `GET` under `/api/v1`: `health`, `config`, `planners/ollama/status`, `history`, `history/{entry_id}`, `runs/{run_id}`, `runs/{run_id}/checkpoint`, `runs/{run_id}/files`, `runs/{run_id}/files/content?path=...`, `runs/{run_id}/plan.md` and `jobs/{job_id}`.
- `jobs/{job_id}` reports the status of a background job from an in-memory job registry. Nothing creates jobs yet (run creation and approval over the API are planned, see `docs/phase-3-api-write-architecture.md`), so every lookup currently answers `404 job_not_found`.
- The OpenAPI description is served at `/api/v1/openapi.json` while the API is running. FastAPI's `/docs` page is switched off on purpose, because it loads scripts from a CDN and this API stays offline.
- Errors always look like `{"error": {"code": "...", "message": "...", "details": ...}}`. Paths in responses are short `~/...` forms, never absolute.
- Tests: `python -m unittest discover -s tests -p "test_api_*.py" -v`. They need the packages above and **fail (they never skip)** if those are missing.

## Where data is stored

| What | Where | Notes |
|---|---|---|
| Run history | `.aether/history.json` in the project folder | Ignored by git. Clear it on the History page, or delete the file. Saving is atomic and locked, so several programs can use it at once; the empty `history.json.lock` beside it is only a lock file and is safe to delete when AETHER is not running |
| Runs and generated projects | `~/AETHER-projects/<run id>/` (`context.json`, `events.jsonl`, `workspace/`) | Always outside AETHER's own folder; AETHER refuses to store runs inside it |

To run a generated project, open its `workspace` folder and follow its README, for example `python calculator.py 12 + 30` or `python server.py`, and `python -m unittest discover -v` for its tests.

## Configuration

All settings are optional environment variables.

| Variable | Default | Purpose |
|---|---|---|
| `AETHER_PLANNER` | `rule` | `rule` for the rule-based planner, `ollama` for the experimental local AI planner. In the web app this sets the default choice |
| `AETHER_RUNS_DIR` | `~/AETHER-projects` | Where runs and generated projects are saved (must be outside the AETHER folder) |
| `AETHER_HISTORY_FILE` | `.aether/history.json` | Where run history is saved (the tests use a temporary file) |

`.streamlit/config.toml` keeps the server on `localhost`, turns off Streamlit's usage statistics and defines the two themes.

## Experimental local AI planner (off by default)

AETHER can ask a small language model running **on your computer** to write the plan, using [Ollama](https://ollama.com) with the `llama3.2:1b` model. AETHER refuses any Ollama address other than `localhost`, and any other model.

**One-time setup** (Windows example):

```bash
winget install --id Ollama.Ollama --exact
ollama pull llama3.2:1b
```

**Turn it on:** in the web app, choose **Local AI: llama3.2:1b (experimental)** under *Planner*. On the command line, set `AETHER_PLANNER=ollama` (PowerShell: `$env:AETHER_PLANNER = "ollama"`).

**What to expect**

- A plan usually takes one to a few minutes and briefly uses about 2 GB of RAM. The model is unloaded straight after each plan.
- Every AI plan is checked against the size of the project: only known phases; no vague, repeated or reworded steps; a sensible balance between phases; and an Implementation step that actually builds each detected feature. The model's answer is capped at 900 tokens, so a repetitive answer is rejected quickly.
- **Automatic fallback:** if Ollama is not running, the model is missing, the answer takes longer than 5 minutes, is cut off, or fails the checks, AETHER uses the rule-based planner instead and shows the reason.
- AI-written steps are labelled *Local AI*. A small model can produce plans that pass the checks but are still incomplete or wrong, so review them before relying on them.

## Run the tests

```bash
python -m unittest discover -s tests -v
```

The suite has 656 tests (250 of them test the web API, which needs `requirements-api.txt`) and takes between one and two minutes.

- The web interface is tested without a browser using Streamlit's `AppTest`. Those tests are skipped if Streamlit is not installed.
- Tests use temporary folders for history, runs and generated files, so they never touch your real data.
- Network access is blocked, and the AI planner is tested with mock model answers, so Ollama is never needed.
- Templates are checked statically: they compile, their imports resolve, they contain no dangerous modules, SQL is parameterised and servers bind to localhost only. AETHER never runs generated code.

## Project layout

```
app.py                      Web interface (Streamlit): pages, layout and navigation
main.py                     Command-line entry point (planning only)
requirements.txt            Dependency for the web interface (streamlit)
requirements-api.txt        Dependencies for the read-only web API and its tests (fastapi, uvicorn, httpx)
.streamlit/config.toml      Local-only server, no usage statistics, dark and light themes
LICENSE                     MIT License
AETHER_DEVELOPMENT_LOG.txt  Status of the latest development task

aether/                     Core library (no UI)
  models.py                 Shared data: TaskBrief, PlanStep, Plan
  manager.py                ManagerAgent: pipeline, checkpoints, approve and resume
  feature_detection.py      Keyword rules that decide which features a requirement or plan step mentions
  schema.py                 Schema versions for saved data, with tolerant, validated loading
  agents.py                 Agent interface (AgentResult) and PlannerAgent
  planner.py                Planner interface and RuleBasedPlanner with its step rules
  ollama_planner.py         Experimental OllamaPlanner (local llama3.2:1b) and its output checks
  fallback.py               FallbackPlanner: tries one planner, uses another if it fails, records why
  config.py                 Chooses the planner from AETHER_PLANNER
  context.py                ProjectContext, activity log (AgentEvent), approvals and RunStore
  developer.py              DeveloperAgent: Checkpoint A preview and template generation
  workspace.py              Workspace: the only safe way to write project files (path checks, size limits)
  fileio.py                 Atomic file writes, retrying reads and a cross-process lock (used by history and RunStore)
  templates/                The three project templates (*.tmpl) and the template-fit rules

aether_ui/                  Web interface helpers
  workflow.py               Runs the pipeline for the UI; progress indicator state
  history.py                Run history in .aether/history.json
  theme.py                  Styling and small HTML components

aether_api/                 Read-only web API (FastAPI) over the same core: no agent logic, no writes
  app.py                    create_app(): routers, security middleware, error handlers
  security.py               Host allow-list and Origin checks (localhost only, no CORS)
  repository.py             Reads runs, history and files through RunStore / history / Workspace
  presenters.py, schemas.py Response models (the API contract) and the mapping from core objects
  jobs.py                   In-memory registry of background jobs (standard library only; nothing starts jobs yet)
  routers/                  health, config, history, runs, files, jobs
docs/                       Planning documents (react-frontend-plan.md, phase-3-api-write-architecture.md)

tests/                      Unit and interface tests (built-in unittest); test_api_*.py cover the web API
```

## Limitations

- **Feature detection is keyword-based.** The Manager recognises login, data storage, web pages and APIs from fixed keywords; it does not understand requirements the way a person would.
- **Only three project types.** The Developer copies a template as it is. Only the generated README mentions your requirement; the code is not adapted to it, and other kinds of software are not built.
- **Reviewer, Tester and Documentation agents are not built.** Generated code is not reviewed, run or tested by AETHER.
- **The AI planner is experimental** and uses a very small model. The rule-based planner is the default.
- **Designed for one person on one computer.** There are no user accounts, history is shared by everyone using the same installation, generated files are only shown in the browser (there is no download), and the server only accepts connections from this computer. It is not ready to be hosted publicly as it is.

## Future improvements

These are planned or possible next steps, not features that exist today.

- Build the Reviewer, Tester and Documentation agents.
- Add write operations to the web API (create, approve and reject runs, with background jobs); the design is in `docs/phase-3-api-write-architecture.md`.
- Build the React web interface described in `docs/react-frontend-plan.md`.
- Support more project types and adapt generated code to the requirement instead of copying a template unchanged.

## Extending AETHER

- **Detect a new feature:** add keywords to `FEATURE_KEYWORDS` in `aether/feature_detection.py`, and steps with the same feature name to `FEATURE_STEPS` in `aether/planner.py`.
- **Add a planner:** subclass `Planner`, implement `create_plan(brief) -> Plan`, add it to `build_planner()` in `aether/config.py`, and wrap it in `FallbackPlanner` to keep the rule-based safety net. The Manager and the data models do not change.
- **Add an agent:** subclass `Agent` in `aether/agents.py` and return an `AgentResult`. Agents never change the shared context directly; the Manager applies their results and records them.

## License and contributions

AETHER is released under the [MIT License](LICENSE). Copyright (c) 2026 Krithick Vivekananda.

It is a student project and is not currently seeking outside contributions. Questions and bug reports are welcome as GitHub issues on the repository. If you fork it, please keep the license notice.
