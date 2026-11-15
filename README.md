# AETHER

**AI-Enabled Team for Hierarchical Engineering and Reasoning**: a multi-agent software engineering assistant, built as a final-year project.

You describe the software you want. A **Manager** agent analyses the requirement, a **Planner** turns it into a phased development plan, you **review and approve** that plan at a checkpoint, and only then does a **Developer** agent write a tested project template into its own folder.

Everything runs on your computer. No API keys, paid services or cloud models are used.

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
git clone <repository-url> AETHER
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

## Where data is stored

| What | Where | Notes |
|---|---|---|
| Run history | `.aether/history.json` in the project folder | Ignored by git. Clear it on the History page, or delete the file |
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

The suite has 155 tests and takes about 10 seconds.

- The web interface is tested without a browser using Streamlit's `AppTest`. Those tests are skipped if Streamlit is not installed.
- Tests use temporary folders for history, runs and generated files, so they never touch your real data.
- Network access is blocked, and the AI planner is tested with mock model answers, so Ollama is never needed.
- Templates are checked statically: they compile, their imports resolve, they contain no dangerous modules, SQL is parameterised and servers bind to localhost only. AETHER never runs generated code.

## Project layout

```
app.py                      Web interface (Streamlit): pages, layout and navigation
main.py                     Command-line entry point (planning only)
requirements.txt            Dependency for the web interface (streamlit)
.streamlit/config.toml      Local-only server, no usage statistics, dark and light themes

aether/                     Core library (no UI)
  models.py                 Shared data: TaskBrief, PlanStep, Plan
  manager.py                ManagerAgent: feature detection, pipeline, checkpoints, approve and resume
  agents.py                 Agent interface (AgentResult) and PlannerAgent
  planner.py                Planner interface and RuleBasedPlanner with its step rules
  ollama_planner.py         Experimental OllamaPlanner (local llama3.2:1b) and its output checks
  fallback.py               FallbackPlanner: tries one planner, uses another if it fails, records why
  config.py                 Chooses the planner from AETHER_PLANNER
  context.py                ProjectContext, activity log (AgentEvent), approvals and RunStore
  developer.py              DeveloperAgent: Checkpoint A preview and template generation
  workspace.py              Workspace: the only safe way to write project files (path checks, size limits)
  templates/                The three project templates (*.tmpl) and the template-fit rules

aether_ui/                  Web interface helpers
  workflow.py               Runs the pipeline for the UI; progress indicator state
  history.py                Run history in .aether/history.json
  theme.py                  Styling and small HTML components

tests/                      Unit and interface tests (built-in unittest)
```

## Limitations

- **Feature detection is keyword-based.** The Manager recognises login, data storage, web pages and APIs from fixed keywords; it does not understand requirements the way a person would.
- **Only three project types.** The Developer copies a template as it is. Only the generated README mentions your requirement; the code is not adapted to it, and other kinds of software are not built.
- **Reviewer, Tester and Documentation agents are not built.** Generated code is not reviewed, run or tested by AETHER.
- **The AI planner is experimental** and uses a very small model. The rule-based planner is the default.
- **Designed for one person on one computer.** There are no user accounts, history is shared by everyone using the same installation, generated files are only shown in the browser (there is no download), and the server only accepts connections from this computer. It is not ready to be hosted publicly as it is.

## Extending AETHER

- **Detect a new feature:** add keywords to `FEATURE_KEYWORDS` in `aether/manager.py`, and steps with the same feature name to `FEATURE_STEPS` in `aether/planner.py`.
- **Add a planner:** subclass `Planner`, implement `create_plan(brief) -> Plan`, add it to `build_planner()` in `aether/config.py`, and wrap it in `FallbackPlanner` to keep the rule-based safety net. The Manager and the data models do not change.
- **Add an agent:** subclass `Agent` in `aether/agents.py` and return an `AgentResult`. Agents never change the shared context directly; the Manager applies their results and records them.
