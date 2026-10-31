# AETHER

**AI-Enabled Team for Hierarchical Engineering and Reasoning**: a multi-agent software engineering assistant (final-year project).

## Stage 1: local rule-based prototype

This stage runs entirely on your computer. It uses only Python's standard library: no AI APIs, no API keys, no internet, and no cost.

Two agents work together:

| Agent | File | Job |
|---|---|---|
| Manager | `aether/manager.py` | Cleans up the requirement, detects features (login, database, web, API) by keyword, and hands a `TaskBrief` to the Planner |
| Planner | `aether/planner.py` | Turns the `TaskBrief` into an ordered `Plan`: base steps for every project plus extra steps for each detected feature |

```
requirement -> ManagerAgent -> TaskBrief -> Planner -> Plan -> printed
```

## Requirements

- Python 3.10 or newer (developed with 3.11)
- Command-line app: nothing to install
- Web app: Streamlit (`pip install -r requirements.txt`)

## Run it (command line)

From this folder:

```bash
python main.py "Build a to-do website where users can log in and save tasks"
```

Or run `python main.py` and type the requirement when asked.

## Run it (web app)

Install the one extra dependency, once:

```bash
python -m pip install -r requirements.txt
```

Then launch the app from this folder:

```bash
python -m streamlit run app.py
```

Your browser opens at http://localhost:8501. The app has three pages, listed in the top navigation:

- **Workspace:** enter a requirement (or pick an example) and click **Generate plan**. The page shows:
  - **Workflow:** the status of each step as it actually ran (done, failed, or not reached). The Code, Review, Test, and Deliver agents are listed as *Not built yet*.
  - **Handoff:** the Manager → Planner → Plan flow and the exact `TaskBrief` the Planner received.
  - **Plan explorer:** all steps, or one tab per phase. Each step is tagged with the rule that added it (Core or a feature). You can download the plan as Markdown.
- **History:** successful runs saved on this computer. **Open** shows a saved run again without re-running it. **Clear history** deletes them.
- **About:** what the prototype does today and what is not built yet.

The app follows your computer's light or dark mode.

To stop the app, press `Ctrl+C` in the terminal. The app runs only on your computer. `.streamlit/config.toml` turns off Streamlit's usage statistics.

Run history is saved in `.aether/history.json` (last 25 runs). Delete that file, or use **Clear history**, to remove it.

## Experimental local AI planner (off by default)

AETHER can ask a small language model running **on this computer** to write the plan instead of the rules. It uses [Ollama](https://ollama.com) with the `llama3.2:1b` model only. No API keys, paid services or cloud models are involved: AETHER refuses any Ollama address other than `localhost`, and any model other than `llama3.2:1b`.

**One-time setup** (already done on the development laptop):

```bash
winget install --id Ollama.Ollama --exact
```

```bash
ollama pull llama3.2:1b
```

**Turn it on or off**

- **Web app:** in the Workspace, choose **Local AI: llama3.2:1b (experimental)** under *Planner*. **Rule-based (default)** turns it off again.
- **Command line:** set `AETHER_PLANNER` before running. `rule` is the default and `ollama` is the experimental planner.

  To turn it on in PowerShell:

  ```powershell
  $env:AETHER_PLANNER = "ollama"; python main.py "Build a to-do website where users can log in and save tasks"
  ```

  To turn it off again:

  ```powershell
  Remove-Item Env:AETHER_PLANNER
  ```

  Setting `AETHER_PLANNER` before `python -m streamlit run app.py` also changes the web app's default choice.

**What to expect**

- A plan usually takes **one to a few minutes**, depending on how busy the computer is, and briefly uses about **2 GB of RAM**. The model is unloaded straight after each plan to give the memory back.
- AETHER checks every AI plan, scaled to the size of the project:
  - **Project tier:** each detected feature adds work (login, storage and API add 2 each; web pages add 1). A total of 0–1 is *Simple*, 2–4 *Moderate* and 5 or more *Complex*. Bigger tiers allow more steps and need more implementation steps (Simple 1, Moderate 2 or one per feature, Complex one per feature plus one).
  - **Clean-up:** it drops unknown phases, vague steps (fewer than 4 words), and repeated or reworded steps. Steps that name different kinds of work, such as security testing and performance testing, are never treated as repeats.
  - **Balance:** no phase may hold more than 45% of the steps (3 are always allowed), and testing plus documentation may not outweigh implementation by more than twice plus two. The prompt suggests tidier targets; the checks only reject clear misses.
  - **Features:** each detected feature needs an Implementation step that actually builds it (for example "Implement…" or "Build…"), and must appear in at least two steps. Mentioning it only in a design step is not enough.
  - **Repetition:** if more than a quarter of the model's steps had to be dropped, the plan is rejected.
  - Every failed check is listed in the reason shown in the app.
- **Length cap:** the model's answer is limited to 900 tokens (a plan needs about 300–600). If it is cut off, the model was most likely repeating itself, and the plan is rejected straight away instead of running until the time limit.
- **Automatic fallback:** if Ollama is not running, the model is missing, the model takes longer than 5 minutes, its answer is cut off, or the plan fails the checks, AETHER uses `RuleBasedPlanner` instead. The web app and command line say so and give the reason.
- **Labels:** the web app always shows which planner produced the plan. AI-written steps are tagged *Local AI*.
- **Caution:** AI-generated plans can pass the checks and still be incomplete or wrong, so review them before relying on them.

## Run the tests

```bash
python -m unittest discover -s tests -v
```

`tests/test_app.py` tests the web page without a browser. It is skipped if Streamlit is not installed. The tests use a temporary history file, so they never touch your real run history.

The AI planner tests (`tests/test_ollama_planner.py`, plus the AI cases in `tests/test_app.py`) use mock model responses. They block real network calls, so they never need Ollama to be installed or running.

## Project layout

```
main.py                 Command-line entry point: reads the requirement, runs the Manager, prints the plan
app.py                  Web interface (Streamlit): pages, layout, and navigation
aether_ui/workflow.py   Runs the Manager and Planner and records what actually happened (statuses, handoff, timing)
aether_ui/history.py    Saves and loads run history in .aether/history.json
aether_ui/theme.py      Styling and small HTML pieces for the web interface
requirements.txt        Extra dependency for the web interface (streamlit)
.streamlit/             Streamlit settings (light/dark theme, local-only, usage statistics off)
aether/models.py     Shared data: TaskBrief, PlanStep, Plan
aether/manager.py    ManagerAgent and the feature keyword rules
aether/planner.py    Planner interface and RuleBasedPlanner with its step rules
aether/ollama_planner.py  Experimental OllamaPlanner (local llama3.2:1b) and its output checks
aether/fallback.py   FallbackPlanner: tries one planner, uses another if it fails, records why
aether/config.py     Chooses the planner from the AETHER_PLANNER setting (rule by default)
tests/               Unit tests (built-in unittest)
```

## Changing the rules

- To detect a new feature, add keywords to `FEATURE_KEYWORDS` in `aether/manager.py`.
- To add steps for that feature, add an entry with the same name to `FEATURE_STEPS` in `aether/planner.py`.

## Path to real AI agents

The Manager only depends on the `Planner` interface (`create_plan(brief) -> Plan`). `OllamaPlanner` is the first planner built this way. To add another one, write a class that inherits from `Planner`, implements `create_plan`, and is added to `build_planner()` in `aether/config.py`. Wrap it in `FallbackPlanner` to keep the rule-based safety net. The Manager, the data models, and the interface stay the same.
