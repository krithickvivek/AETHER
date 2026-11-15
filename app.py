"""AETHER web workspace (Streamlit).

Launch from the project folder with:
    python -m streamlit run app.py

All planning comes from the aether package (ManagerAgent + the selected planner).
This file only handles layout, navigation and display.
"""

from datetime import datetime
from pathlib import Path

import streamlit as st

import hashlib

from aether.config import build_planner, planner_setting
from aether.context import AWAITING_APPROVAL, CANCELLED, COMPLETED, ProjectContext, RunStore
from aether.developer import preview
from aether.ollama_planner import DEFAULT_MODEL, ollama_status
from aether.templates import AMBIGUOUS, POOR_FIT, TemplateChoice
from aether.workspace import Workspace
from aether_ui import history, theme
from aether_ui.workflow import (
    FEATURE_LABELS, RunResult, decide_plan, format_duration, group_by_phase, plan_to_markdown, progress_steps,
    start_project,
)

PLANNER_LABELS = {
    "rule": "Rule-based (default)",
    "ollama": f"Local AI: {DEFAULT_MODEL} (experimental)",
}
AI_CAUTION = "AI-generated plans can be incomplete or wrong. Review this plan before relying on it."

EXAMPLES = {
    "To-do web app": "Build a to-do website where users can log in and save their tasks",
    "Library REST API": "Create a REST API for a library that stores books and members in a database",
    "CLI calculator": "A command-line calculator for basic arithmetic",
}
EXAMPLE_ICONS = {
    "To-do web app": ":material/checklist:",
    "Library REST API": ":material/local_library:",
    "CLI calculator": ":material/calculate:",
}


def display_path(path: Path) -> str:
    """A short form of a local folder for the page: no user name or full path.

    ~/AETHER-projects/<run id>/workspace for the default location, otherwise only the last three folders.
    """
    path = Path(path)
    try:
        relative = path.resolve().relative_to(Path.home().resolve())
        if len(relative.parts) <= 4:
            return "~/" + relative.as_posix()
    except ValueError:
        pass
    return "…/" + "/".join(path.parts[-3:])


def format_time(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%d %b %Y, %H:%M")


def use_example() -> None:
    choice = st.session_state.get("example")
    if choice:
        st.session_state["requirement"] = EXAMPLES[choice]
    st.session_state["example"] = None


def default_planner_choice() -> str:
    try:
        return planner_setting()
    except ValueError:
        return "rule"


@st.cache_data(ttl=15, show_spinner=False)
def cached_ollama_status() -> tuple[bool, str]:
    return ollama_status()


def render_planner_choice() -> str:
    choice = st.radio(
        "Planner",
        list(PLANNER_LABELS),
        format_func=PLANNER_LABELS.get,
        index=list(PLANNER_LABELS).index(default_planner_choice()),
        key="planner_choice",
        horizontal=True,
    )
    if choice == "ollama":
        ready, status = cached_ollama_status()
        st.caption(
            f"{'●' if ready else '○'} {status} Runs only on this computer, with no paid or cloud services. "
            "A plan usually takes one to a few minutes, depending on how busy this computer is. If the model fails or "
            "its plan does not pass AETHER's quality checks, the rule-based planner is used instead and the reason is shown."
        )
    return choice


LANGUAGES = {"py": "python", "sql": "sql", "md": "markdown", "css": "css", "html": "html", "json": "json"}


def run_store() -> RunStore | None:
    """Where runs and generated projects are saved (outside AETHER's own folder)."""
    try:
        return RunStore()
    except ValueError as error:
        st.error(f"Generated projects cannot be saved: {error}", icon=":material/error:")
        return None


def generate(choice: str) -> None:
    """Run the real workflow up to Checkpoint A and remember the result."""
    if choice == "ollama":
        message = f"The local model ({DEFAULT_MODEL}) is writing the plan. This can take a few minutes..."
    else:
        message = "Running Manager and Planner..."
    store = run_store()
    with st.spinner(message):
        result, context = start_project(st.session_state.get("requirement", ""), planner=build_planner(choice),
                                        store=store)
    st.session_state["result"] = result
    st.session_state["context"] = context if store else None
    st.session_state["result_source"] = "live"
    if result.ok:
        try:
            history.save_run(result)
            st.toast("Plan saved to history", icon=":material/history:")
        except OSError:
            st.toast("The plan was generated but could not be saved to history.", icon=":material/warning:")


# ------------------------------------------------------------------ workspace

def workspace_page() -> None:
    # Compact once a run is on screen, including the rerun in which Generate was just clicked.
    theme.hero(compact=st.session_state.get("result") is not None or bool(st.session_state.get("generate")))

    with st.container(key="ae_input"):  # styled as one input panel (.st-key-ae_input in theme.py)
        theme.input_header("Requirement", "Mention login, saving data, web pages or an API to add those steps.")
        st.pills("Start from an example", list(EXAMPLES), key="example", on_change=use_example,
                 format_func=lambda name: f"{EXAMPLE_ICONS[name]} {name}",
                 help="Fills in a requirement that matches one of AETHER's three project templates. "
                      "You can edit it before generating.")
        st.text_area(
            "Software requirement",
            key="requirement",
            height=150,
            max_chars=2000,
            placeholder="Describe the software you want to build, e.g. a to-do website where users can log in "
                        "and save their tasks.",
            label_visibility="collapsed",
        )
        planner_col, button_col = st.columns([3, 1], vertical_alignment="bottom")
        with planner_col:
            choice = render_planner_choice()
        with button_col:
            clicked = st.button("Generate plan", key="generate", type="primary",
                                icon=":material/arrow_forward:", width="stretch")

    if clicked:
        generate(choice)

    result: RunResult | None = st.session_state.get("result")
    if result is None:
        theme.empty_state(
            "No plan yet",
            "Enter a requirement and select Generate plan. Every status below the input "
            "reflects what the Manager and Planner actually did.",
            show_flow=True,
        )
        return

    render_result(result)


def render_result(result: RunResult) -> None:
    st.divider()
    theme.progress(progress_steps(result, st.session_state.get("context")))

    if not result.ok:
        st.error(result.error, icon=":material/error:")
        if result.error_details:
            with st.expander("Technical details"):
                st.code(result.error_details, language=None)
    else:
        if st.session_state.get("result_source") == "history":
            st.info(f"Showing a saved run from {format_time(result.started_at)}. "
                    "Statuses are from that run; nothing was re-executed.", icon=":material/history:")
        else:
            kind = f"local AI model {DEFAULT_MODEL}" if result.ai_generated else "rules"
            st.success(f"Plan generated by {result.plan.planner_name} ({kind}) in {format_duration(result.duration_ms)}.",
                       icon=":material/check_circle:")
        if result.fallback_reason:
            st.warning(f"The local AI planner did not produce a usable plan: {result.fallback_reason} "
                       f"This plan was made by {result.plan.planner_name} instead.", icon=":material/swap_horiz:")
        if result.ai_generated:
            st.warning(AI_CAUTION, icon=":material/info:")

    if result.ok:
        cols = st.columns(4)
        cols[0].metric("Features detected", len(result.brief.features))
        cols[1].metric("Plan steps", len(result.plan.steps))
        cols[2].metric("Phases", len(group_by_phase(result.plan.steps)))
        cols[3].metric("Run time", format_duration(result.duration_ms))

    context: ProjectContext | None = st.session_state.get("context")

    # The decision comes first (Checkpoint A, or the files it produced), then the plan it is about.
    if context is not None and context.plan is not None:
        render_development(context)
        st.divider()

    if result.ok:
        theme.label("Detected features")
        theme.feature_chips(result.brief.features)
        render_plan_explorer(result)
    else:
        theme.label("Plan explorer")
        theme.empty_state("No plan produced", "The Manager stopped before handing off, so the Planner did not run.")

    render_under_the_hood(result, context)


def render_under_the_hood(result: RunResult, context: ProjectContext | None) -> None:
    """Agent activity, the Manager -> Planner handoff, the TaskBrief and the Manager log, collapsed by default."""
    with st.expander("Under the hood: agent activity, handoff and TaskBrief", icon=":material/terminal:"):
        activity_col, handoff_col = st.columns([5, 7], gap="large")
        with activity_col:
            theme.label("Agent activity")
            if context is not None:
                theme.events(context.events)  # the run's real activity log, including approvals
            else:
                theme.stages(result.stages)  # runs saved before the activity log existed
        with handoff_col:
            theme.label("Handoff")
            theme.handoff(result.stages[-1].agent, result.brief, result.plan)
            if result.brief:
                # The actual object the Planner received, captured by TracingPlanner.
                st.code(f"TaskBrief(\n  requirement={result.brief.requirement!r},\n"
                        f"  features={result.brief.features!r},\n)", language="python", wrap_lines=True)
            theme.label("Manager log")
            st.code("\n".join(result.log) or "(no messages)", language=None, wrap_lines=True)


def render_development(context: ProjectContext) -> None:
    """Checkpoint A before generation, then the files the Developer actually wrote."""
    if context.status == AWAITING_APPROVAL:
        render_checkpoint(context)
    elif context.status == CANCELLED:
        theme.label("Checkpoint A")
        st.info("You rejected this plan, so no project files were generated. Generate a new plan to try again.",
                icon=":material/block:")
    elif context.files:
        render_files(context)
    else:
        failure = next((e for e in reversed(context.events) if e.agent == "Developer"), None)
        if failure is not None:
            theme.label("Project files")
            st.error(f"The Developer could not generate this project. {failure.detail}", icon=":material/error:")


def feature_names(features: list[str]) -> str:
    return ", ".join(FEATURE_LABELS.get(f, f) for f in features) or "none"


def render_fit(choice: TemplateChoice) -> None:
    """Why this project type was (or was not) chosen, and which requested features it covers."""
    lines = []
    if choice.evidence:
        lines.append("- Names the project type: " + ", ".join(f"`{w}`" for w in choice.evidence))
    else:
        lines.append("- No words in the requirement name a supported project type.")
    if choice.hints:
        lines.append("- Related words (not enough on their own): " + ", ".join(f"`{w}`" for w in choice.hints))
    if choice.template is not None or choice.status == POOR_FIT:
        lines.append(f"- Supported requirements: {feature_names(choice.supported)}")
    lines.append(f"- Not supported: {feature_names(choice.unsupported)}")
    st.markdown("**Template fit**\n\n" + "\n".join(lines))


def render_override(context: ProjectContext, choice: TemplateChoice, store: RunStore) -> None:
    """An explicit, acknowledged override for when no template was chosen automatically."""
    with st.expander("Override: build a template anyway (not recommended)", icon=":material/warning:"):
        st.markdown("AETHER did not choose a template because it does not clearly fit this requirement. "
                    "An override generates a tested template **as it is**: it is not adapted to your requirement.")
        titles = {t.key: t.title for t in choice.candidates}
        key = st.selectbox("Template to build", list(titles), format_func=titles.get,
                           key=f"override_template_{context.run_id}")
        forced = preview(context, override=key)
        consequences = [f"The project will be a **{titles[key]}**, whatever the requirement asks for."]
        if forced.choice.unsupported:
            consequences.append(f"These requested features will **not** be built: "
                                f"{feature_names(forced.choice.unsupported)}.")
        if forced.uncovered_steps:
            consequences.append(f"Plan steps {', '.join(map(str, forced.uncovered_steps))} will have no file.")
        consequences.append("The run is recorded as an explicit override in the activity log.")
        st.markdown("\n".join(f"- {line}" for line in consequences))
        acknowledged = st.checkbox("I understand these consequences and want to generate this template anyway.",
                                   key=f"override_ack_{context.run_id}")
        if st.button(f"Override and generate {titles[key]}", key="override_plan", disabled=not acknowledged,
                     icon=":material/warning:"):
            with st.spinner("Developer is writing the project files..."):
                st.session_state["context"] = decide_plan(context, approved=True, store=store, override=key,
                                                          note="Explicit template override in the web app")
            st.rerun()


def fit_badge(plan_preview) -> tuple[str, str]:
    """(label, tone) for the template-fit status shown in the checkpoint header."""
    choice = plan_preview.choice
    if plan_preview.template is not None:
        return ("Partial fit", "warn") if choice.unsupported else ("Fits the requirement", "ok")
    if choice.status == POOR_FIT:
        return "Poor fit", "fail"
    if choice.status == AMBIGUOUS:
        return "Ambiguous: more than one project type", "warn"
    return "No matching template", "fail"


def render_checkpoint(context: ProjectContext) -> None:
    plan_preview = preview(context)
    template = plan_preview.template
    store = run_store()
    plan_steps = len(context.plan.steps)
    file_count = len(plan_preview.files)

    with st.container(key="ae_checkpoint"):  # highlighted panel (.st-key-ae_checkpoint in theme.py)
        theme.checkpoint_header("Checkpoint A · Approve the plan", [
            fit_badge(plan_preview),
            (f"{file_count} files planned" if template is not None else "No files planned", "neutral"),
            ("Nothing written yet", "neutral"),
        ])
        if template is None:
            st.warning(f"{plan_preview.choice.reason} The Developer only writes projects from tested templates "
                       "that clearly fit, so it will not build this one automatically.", icon=":material/report:")
            render_fit(plan_preview.choice)
        else:
            st.markdown(f"**{template.title}** · {template.kind}  \n{template.description}")
            fit_col, files_col = st.columns([5, 7], gap="large")
            with fit_col:
                render_fit(plan_preview.choice)
                if plan_preview.choice.unsupported:
                    missing = ", ".join(FEATURE_LABELS.get(f, f) for f in plan_preview.choice.unsupported)
                    st.warning(f"This template does not include: {missing}. Those parts of the plan will not be built.",
                               icon=":material/report:")
                if plan_preview.uncovered_steps:
                    steps = ", ".join(map(str, plan_preview.uncovered_steps))
                    st.warning(f"No generated file carries out plan step(s) {steps}.", icon=":material/report:")
            with files_col:
                rows = [{"File": path, "Plan steps": ", ".join(map(str, plan_preview.covers.get(path, []))) or "-"}
                        for path in plan_preview.files]
                st.dataframe(rows, hide_index=True, width="stretch")
                if store is not None:
                    st.caption(f"{file_count} files will be written to {display_path(store.workspace_dir(context.run_id))}. "
                               "Generated code is not run.")

        approve_col, reject_col, note_col = st.columns([2, 2, 3], vertical_alignment="center")
        if approve_col.button("Approve and generate files", key="approve_plan", type="primary",
                              disabled=template is None or store is None, icon=":material/check:", width="stretch"):
            with st.spinner("Developer is writing the project files..."):
                st.session_state["context"] = decide_plan(context, approved=True, store=store)
            st.rerun()
        if reject_col.button("Reject plan", key="reject_plan", disabled=store is None, icon=":material/close:",
                             width="stretch"):
            st.session_state["context"] = decide_plan(context, approved=False, store=store,
                                                      note="Rejected in the web app")
            st.rerun()
        note_col.caption(f"Review the {plan_steps} plan steps below before deciding. "
                         "Rejecting cancels the run and writes nothing.")
        if plan_preview.choice.can_override and store is not None:
            render_override(context, plan_preview.choice, store)


def render_files(context: ProjectContext) -> None:
    store = run_store()
    if store is None:
        return
    workspace = Workspace(store.workspace_dir(context.run_id))
    developer = next((e for e in reversed(context.events) if e.agent == "Developer"), None)
    by_path = {record.path: record for record in context.files}
    total = sum(record.size for record in context.files)

    with st.container(key="ae_files"):  # panel (.st-key-ae_files in theme.py)
        theme.checkpoint_header("Generated files", [
            (context.notes.get("developer_template", "Project"), "ok"),
            (f"{len(by_path)} files · {total:,} bytes", "neutral"),
            ("Not run", "neutral"),
        ])
        if context.status == COMPLETED and developer is not None:
            st.success(f"The Developer generated {developer.detail.split(';', 1)[0]}.", icon=":material/folder_code:")
        if context.notes.get("developer_override"):
            st.warning(context.notes["developer_override"], icon=":material/warning:")
        for note in ("developer_unsupported", "developer_uncovered_steps"):
            if context.notes.get(note):
                label = "Not covered by the template" if note == "developer_unsupported" else "Plan steps without a file"
                st.warning(f"{label}: {context.notes[note]}", icon=":material/report:")

        choice = st.selectbox("File", sorted(by_path), key="file_choice",
                              format_func=lambda path: f"{path}  ·  {by_path[path].size:,} bytes")
        record = by_path[choice]
        try:
            text = workspace.read_text(choice)
        except OSError:
            st.error("This file is no longer on disk.", icon=":material/error:")
            return
        changed = hashlib.sha256(text.encode("utf-8")).hexdigest() != record.sha256
        language = LANGUAGES.get(choice.rsplit(".", 1)[-1])
        theme.file_header(record, language, changed)
        if changed:
            st.warning("This file has changed since the Developer wrote it.", icon=":material/report:")
        st.code(text or "(empty file)", language=language, line_numbers=True)
        st.caption(f"Saved in {display_path(workspace.root)}. Generated code has not been run, reviewed or tested yet.")


def render_plan_explorer(result: RunResult) -> None:
    plan = result.plan
    groups = group_by_phase(plan.steps)
    numbered = list(enumerate(plan.steps, start=1))

    theme.label("Plan explorer")
    tabs = st.tabs([f"All {len(plan.steps)}"] + [f"{phase} {len(steps)}" for phase, steps in groups.items()])
    with tabs[0]:
        theme.step_list(numbered, plan.planner_name, show_phase=True)
    for tab, phase in zip(tabs[1:], groups):
        with tab:
            theme.step_list([(n, s) for n, s in numbered if s.phase == phase], plan.planner_name, show_phase=False)

    st.download_button("Download plan (Markdown)", data=plan_to_markdown(plan), file_name="aether_plan.md",
                       mime="text/markdown", icon=":material/download:", on_click="ignore")


# -------------------------------------------------------------------- history

def history_page() -> None:
    theme.page_title("History", "Plans generated on this computer. Saved locally in .aether/history.json.")
    entries = history.load_history()
    if not entries:
        theme.empty_state("No saved runs yet", "Successful runs from the Workspace appear here.")
        return

    count_col, clear_col = st.columns([3, 1], vertical_alignment="center")
    count_col.caption(f"{len(entries)} saved run{'s' if len(entries) != 1 else ''} (newest first, up to {history.MAX_ENTRIES})")
    with clear_col.popover("Clear history", icon=":material/delete:", width="stretch"):
        st.write("Delete all saved runs from this computer?")
        if st.button("Delete all", type="primary", key="confirm_clear"):
            history.clear_history()
            st.rerun()

    for entry in entries:
        result = history.entry_to_result(entry)
        features = ", ".join(FEATURE_LABELS.get(f, f) for f in result.brief.features) or "core steps only"
        planner = result.plan.planner_name + (" (after AI fallback)" if result.fallback_reason else "")
        meta = f"{format_time(result.started_at)} · {planner} · {len(result.plan.steps)} steps · {features}"
        with st.container(border=True):
            text_col, button_col = st.columns([5, 1], vertical_alignment="center")
            with text_col:
                theme.history_row(result.requirement, meta)
            if button_col.button("Open", key=f"open-{entry['id']}", width="stretch"):
                st.session_state["result"] = result
                st.session_state["context"] = load_context(result.run_id)
                st.session_state["result_source"] = "history"
                st.session_state["requirement"] = result.requirement
                st.switch_page(WORKSPACE)


def load_context(run_id: str | None) -> ProjectContext | None:
    """The saved run behind a history entry (events, approval, files), if it still exists."""
    if not run_id:
        return None
    try:
        return RunStore().load(run_id)
    except (OSError, ValueError, KeyError):
        return None


# ---------------------------------------------------------------------- about

def about_page() -> None:
    theme.page_title("About this prototype", "What AETHER does today, and what it does not do yet.")
    st.markdown(
        """
**AETHER** (AI-Enabled Team for Hierarchical Engineering and Reasoning) is a final-year project
for a multi-agent software engineering assistant. This is **Stage 1: a local prototype**. It is
rule-based by default, with an optional experimental local AI planner.

| Agent | Status | How it works today |
|---|---|---|
| Manager | Available | Rule-based: understands the requirement and coordinates the agents |
| Planner | Available | Rule-based (default), or an experimental local AI Planner |
| Developer | Available | Writes project files from tested templates (calculator, to-do web app, library API) after you approve the plan |
| Reviewer | Not built yet | |
| Tester | Not built yet | |
| Documentation | Not built yet | |

**What happens when you generate a plan**

1. The **ManagerAgent** cleans up the requirement and detects features (login, storage, web interface, API) by matching keywords.
2. It hands a **TaskBrief** to the **RuleBasedPlanner**.
3. The Planner combines core steps with extra steps for each detected feature, ordered by phase.
4. **Checkpoint A:** the run pauses. You see the project type and the files the Developer would write, then approve or reject the plan.
5. After approval, the **DeveloperAgent** copies a tested template into a new project folder outside AETHER
   (`~/AETHER-projects/<run id>/workspace`) and records each file's checksum and the plan steps it carries out.
   The Developer never asks an AI model to write code, and generated code is not run.

With the default rule-based planner, no AI model is used, and each step in the plan explorer is tagged
with the rule that added it, so every result can be explained.

**Experimental local AI planner.** Choosing *Local AI* asks a small language model
(Llama 3.2 1B, running in Ollama on this computer) to write the plan. AETHER checks the answer against the size of the
project: only known phases, no vague or repeated steps, a sensible balance between phases, and an implementation
step that actually builds each detected feature. If Ollama is not
running, the request times out, or the plan fails these checks, AETHER uses the rule-based planner instead
and shows the reason. A small model can still produce plans that pass the checks but are incomplete or
wrong, so review AI-generated plans before relying on them.

No API keys, paid services or cloud models are used, and run history stays on this computer.
The Planner follows a small interface (`create_plan(brief) -> Plan`), which is what allows the planners to
be swapped without changing the Manager.
        """
    )


# ------------------------------------------------------------------- app shell

st.set_page_config(page_title="AETHER", page_icon=":material/hub:", layout="wide")

WORKSPACE = st.Page(workspace_page, title="Workspace", icon=":material/space_dashboard:", default=True)
pages = [
    WORKSPACE,
    st.Page(history_page, title="History", icon=":material/history:", url_path="history"),
    st.Page(about_page, title="About", icon=":material/info:", url_path="about"),
]
navigation = st.navigation(pages, position="top")

theme.inject_css()
theme.brand_bar()
navigation.run()
