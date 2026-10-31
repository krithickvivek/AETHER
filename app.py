"""AETHER web workspace (Streamlit).

Launch from the project folder with:
    python -m streamlit run app.py

All planning comes from the aether package (ManagerAgent + the selected planner).
This file only handles layout, navigation and display.
"""

from datetime import datetime

import streamlit as st

from aether.config import build_planner, planner_setting
from aether.ollama_planner import DEFAULT_MODEL, ollama_status
from aether_ui import history, theme
from aether_ui.workflow import (
    FEATURE_LABELS, RunResult, format_duration, group_by_phase, plan_to_markdown, run_workflow,
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


def generate(choice: str) -> None:
    """Run the real workflow once and remember the result."""
    if choice == "ollama":
        message = f"The local model ({DEFAULT_MODEL}) is writing the plan. This can take a few minutes..."
    else:
        message = "Running Manager and Planner..."
    with st.spinner(message):
        result = run_workflow(st.session_state.get("requirement", ""), planner=build_planner(choice))
    st.session_state["result"] = result
    st.session_state["result_source"] = "live"
    if result.ok:
        try:
            history.save_run(result)
            st.toast("Plan saved to history", icon=":material/history:")
        except OSError:
            st.toast("The plan was generated but could not be saved to history.", icon=":material/warning:")


# ------------------------------------------------------------------ workspace

def workspace_page() -> None:
    theme.hero()

    theme.label("Requirement")
    st.text_area(
        "Software requirement",
        key="requirement",
        height=130,
        max_chars=2000,
        placeholder="Describe the software you want to build, e.g. a to-do website where users can log in and save tasks.",
        label_visibility="collapsed",
    )
    left, right = st.columns([3, 1], vertical_alignment="bottom")
    with left:
        st.pills("Try an example", list(EXAMPLES), key="example", on_change=use_example)
    with right:
        clicked = st.button("Generate plan", key="generate", type="primary",
                            icon=":material/arrow_forward:", width="stretch")
    choice = render_planner_choice()

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

    flow_col, plan_col = st.columns([5, 7], gap="large")
    planner_name = result.stages[-1].agent

    with flow_col:
        theme.label("Workflow")
        theme.stages(result.stages)

        theme.label("Handoff")
        theme.handoff(planner_name, result.brief, result.plan)
        if result.brief:
            # The actual object the Planner received, captured by TracingPlanner.
            st.code(f"TaskBrief(\n  requirement={result.brief.requirement!r},\n"
                    f"  features={result.brief.features!r},\n)", language="python", wrap_lines=True)
        with st.expander("Manager log"):
            st.code("\n".join(result.log) or "(no messages)", language=None, wrap_lines=True)

    with plan_col:
        if not result.ok:
            theme.label("Plan explorer")
            theme.empty_state("No plan produced",
                              "The Manager stopped before handing off, so the Planner did not run.")
            return
        theme.label("Detected features")
        theme.feature_chips(result.brief.features)
        render_plan_explorer(result)


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
                st.session_state["result_source"] = "history"
                st.session_state["requirement"] = result.requirement
                st.switch_page(WORKSPACE)


# ---------------------------------------------------------------------- about

def about_page() -> None:
    theme.page_title("About this prototype", "What AETHER does today, and what it does not do yet.")
    st.markdown(
        """
**AETHER** (AI-Enabled Team for Hierarchical Engineering and Reasoning) is a final-year project
for a multi-agent software engineering assistant. This is **Stage 1: a local prototype**. It is
rule-based by default, with an optional experimental local AI planner.

| Stage | Status | How it works today |
|---|---|---|
| Plan | Available | Rule-based Manager and Planner (default), or an experimental local AI Planner |
| Code | Not built yet | |
| Review | Not built yet | |
| Test | Not built yet | |
| Deliver | Not built yet | |

**What happens when you generate a plan**

1. The **ManagerAgent** cleans up the requirement and detects features (login, storage, web interface, API) by matching keywords.
2. It hands a **TaskBrief** to the **RuleBasedPlanner**.
3. The Planner combines core steps with extra steps for each detected feature, ordered by phase.

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
