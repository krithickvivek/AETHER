"""AETHER visual layer: CSS and small HTML building blocks.

Colours follow the active Streamlit theme (light or dark). All user text is
escaped before it is placed in HTML.
"""

from html import escape
from string import Template

import streamlit as st

from aether.context import AgentEvent
from aether.models import Plan, TaskBrief
from aether.planner import RuleBasedPlanner
from aether_ui.workflow import FEATURE_LABELS, Stage, step_origin

LIGHT = {
    "accent": "#6D28D9", "accent_soft": "rgba(109, 40, 217, 0.07)",
    "border": "#E4E4E7", "muted": "#71717A", "ok": "#16A34A", "fail": "#DC2626",
}
DARK = {
    "accent": "#A78BFA", "accent_soft": "rgba(167, 139, 250, 0.10)",
    "border": "#27272A", "muted": "#A1A1AA", "ok": "#4ADE80", "fail": "#F87171",
}

# Agents planned for later stages of the project. Shown as "Not built yet".
PLANNED_STAGES = [
    ("Reviewer", "Review code"),
    ("Tester", "Write and run tests"),
    ("Documentation", "Write project documentation"),
]

CSS = Template("""
<style>
:root {
  --ae-accent: $accent; --ae-accent-soft: $accent_soft; --ae-border: $border;
  --ae-muted: $muted; --ae-ok: $ok; --ae-fail: $fail;
}
.block-container { max-width: 1120px; padding-top: 4.5rem; padding-bottom: 4rem; }
h1, h2, h3 { letter-spacing: -0.015em; }

.ae-bar { display: flex; align-items: center; justify-content: space-between; gap: 1rem;
  flex-wrap: wrap; padding-bottom: 1rem; border-bottom: 1px solid var(--ae-border); margin-bottom: 0.5rem; }
.ae-brand { display: flex; align-items: center; gap: 0.75rem; min-width: 0; }
.ae-mark { width: 32px; height: 32px; border-radius: 8px; background: var(--ae-accent); color: #fff;
  display: grid; place-items: center; font-weight: 700; font-size: 0.95rem; flex: none; }
.ae-name { font-size: 1.05rem; font-weight: 700; letter-spacing: 0.18em; line-height: 1.1; }
.ae-expansion { color: var(--ae-muted); font-size: 0.78rem; line-height: 1.3; }
.ae-badge { display: inline-flex; align-items: center; gap: 0.4rem; padding: 0.2rem 0.65rem;
  border: 1px solid var(--ae-border); border-radius: 999px; font-size: 0.75rem; color: var(--ae-muted); white-space: nowrap; }
.ae-badge::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: var(--ae-accent); }

.ae-hero { padding: 1.75rem 0 0.75rem; }
.ae-tagline { font-size: clamp(1.7rem, 3.4vw, 2.5rem); font-weight: 650; letter-spacing: -0.025em;
  line-height: 1.12; margin: 0 0 0.6rem; }
.ae-verbs { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.35rem 1rem; color: var(--ae-muted); font-size: 0.95rem; }
.ae-verbs .on { color: var(--ae-accent); font-weight: 600; }
.ae-verbs .note { font-size: 0.78rem; }

.ae-label { text-transform: uppercase; letter-spacing: 0.08em; font-size: 0.7rem; font-weight: 600;
  color: var(--ae-muted); margin: 0.25rem 0 0.6rem; }
.ae-page-title { font-size: 1.6rem; font-weight: 650; letter-spacing: -0.02em; margin: 1.5rem 0 0.25rem; }
.ae-page-sub { color: var(--ae-muted); font-size: 0.92rem; margin-bottom: 1rem; }

.ae-stages, .ae-steps { list-style: none; padding-left: 0 !important; margin-left: 0 !important; }
.ae-stage, .ae-step { margin-left: 0 !important; }
.ae-stages { margin: 0 0 1.5rem; }
.ae-stage { display: flex; gap: 0.75rem; padding: 0.6rem 0; border-bottom: 1px solid var(--ae-border); }
.ae-stage:last-child { border-bottom: none; }
.ae-dot { flex: 0 0 22px; height: 22px; border-radius: 50%; display: grid; place-items: center;
  font-size: 0.7rem; font-weight: 700; border: 1.5px solid var(--ae-border); color: var(--ae-muted); margin-top: 1px; }
.ae-dot.done { background: var(--ae-ok); border-color: var(--ae-ok); color: #fff; }
.ae-dot.failed { background: var(--ae-fail); border-color: var(--ae-fail); color: #fff; }
.ae-dot.planned { border-style: dashed; }
.ae-dot.waiting { border-color: var(--ae-accent); color: var(--ae-accent); }
.ae-stage-title { font-weight: 600; font-size: 0.9rem; }
.ae-stage-agent { color: var(--ae-muted); font-weight: 500; }
.ae-stage-detail { color: var(--ae-muted); font-size: 0.8rem; }
.ae-stage.planned .ae-stage-title { color: var(--ae-muted); font-weight: 500; }
.ae-tag { font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--ae-muted);
  border: 1px solid var(--ae-border); border-radius: 4px; padding: 0 0.3rem; margin-left: 0.35rem; }

.ae-flow { display: flex; flex-direction: column; gap: 0.2rem; margin-bottom: 0.75rem; }
.ae-node { border: 1px solid var(--ae-border); border-radius: 10px; padding: 0.5rem 0.75rem;
  display: flex; justify-content: space-between; align-items: baseline; gap: 0.75rem; }
.ae-node.active { border-color: var(--ae-accent); background: var(--ae-accent-soft); }
.ae-node.off { opacity: 0.45; }
.ae-node-role { font-size: 0.66rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--ae-muted); }
.ae-node-name { font-weight: 600; font-size: 0.86rem; }
.ae-arrow { display: flex; align-items: center; gap: 0.5rem; color: var(--ae-accent);
  font-size: 0.72rem; font-family: ui-monospace, monospace; padding: 0.1rem 0 0.1rem 1rem; }
.ae-arrow.off { color: var(--ae-muted); opacity: 0.6; }

.ae-chip { display: inline-block; padding: 0.12rem 0.55rem; border-radius: 999px; font-size: 0.76rem;
  border: 1px solid var(--ae-border); color: var(--ae-muted); margin: 0 0.35rem 0.35rem 0; white-space: nowrap; }
.ae-chip.accent { border-color: var(--ae-accent); color: var(--ae-accent); background: var(--ae-accent-soft); }

.ae-steps { list-style: none; padding: 0; margin: 0; }
.ae-step { display: grid; grid-template-columns: 2rem 1fr auto; gap: 0.75rem; align-items: baseline;
  padding: 0.6rem 0; border-bottom: 1px solid var(--ae-border); }
.ae-step:last-child { border-bottom: none; }
.ae-step-num { color: var(--ae-muted); font-variant-numeric: tabular-nums; font-size: 0.82rem; }
.ae-step-phase { display: block; font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--ae-muted); }
.ae-step .ae-chip { margin: 0; }

.ae-empty { border: 1px dashed var(--ae-border); border-radius: 12px; padding: 2rem 1.5rem; text-align: center; margin-top: 1rem; }
.ae-empty-title { font-weight: 600; margin-bottom: 0.35rem; }
.ae-empty-text { color: var(--ae-muted); font-size: 0.9rem; max-width: 34rem; margin: 0 auto; }
.ae-empty-steps { display: flex; justify-content: center; flex-wrap: wrap; gap: 0.5rem 1.25rem; margin-top: 1rem;
  font-size: 0.82rem; color: var(--ae-muted); }
.ae-empty-steps b { color: var(--ae-accent); margin-right: 0.3rem; }

.ae-history-req { font-weight: 600; font-size: 0.92rem; overflow-wrap: anywhere; }
.ae-history-meta { color: var(--ae-muted); font-size: 0.8rem; margin-top: 0.2rem; }

@media (max-width: 640px) {
  .ae-step { grid-template-columns: 1.6rem 1fr; }
  .ae-step .ae-chip { grid-column: 2; justify-self: start; }
  .ae-expansion { display: none; }
}
</style>
""")


def palette() -> dict:
    """Colours for the active Streamlit theme (light if it can't be detected)."""
    try:
        kind = st.context.theme.type
    except Exception:
        kind = None
    return DARK if kind == "dark" else LIGHT


def inject_css() -> None:
    st.html(CSS.substitute(palette()))


def brand_bar() -> None:
    st.html("""
    <div class="ae-bar">
      <div class="ae-brand">
        <div class="ae-mark">A</div>
        <div>
          <div class="ae-name">AETHER</div>
          <div class="ae-expansion">AI-Enabled Team for Hierarchical Engineering and Reasoning</div>
        </div>
      </div>
      <span class="ae-badge">Local prototype &middot; rule-based by default</span>
    </div>
    """)


def hero() -> None:
    st.html("""
    <div class="ae-hero">
      <div class="ae-tagline">Intelligence that engineers together.</div>
      <div class="ae-verbs">
        <span class="on">Plan.</span><span class="on">Code.</span><span>Review.</span><span>Test.</span><span>Deliver.</span>
        <span class="note">Planning and project generation are available today. Review, testing and delivery are not built yet.</span>
      </div>
    </div>
    """)


def label(text: str) -> None:
    st.html(f'<div class="ae-label">{escape(text)}</div>')


def page_title(title: str, subtitle: str) -> None:
    st.html(f'<div class="ae-page-title">{escape(title)}</div><div class="ae-page-sub">{escape(subtitle)}</div>')


def empty_state(title: str, text: str, show_flow: bool = False) -> None:
    flow = ""
    if show_flow:
        flow = """
        <div class="ae-empty-steps">
          <span><b>1</b>Manager analyses the requirement</span>
          <span><b>2</b>Hands a TaskBrief to the Planner</span>
          <span><b>3</b>Planner returns a phased plan</span>
          <span><b>4</b>You approve it, the Developer writes the files</span>
        </div>"""
    st.html(f"""
    <div class="ae-empty">
      <div class="ae-empty-title">{escape(title)}</div>
      <div class="ae-empty-text">{escape(text)}</div>{flow}
    </div>
    """)


ICONS = {"done": "&#10003;", "failed": "&#10005;", "skipped": "&ndash;", "waiting": "&#8230;"}


def _planned_rows() -> list[str]:
    return [f"""
        <li class="ae-stage planned">
          <div class="ae-dot planned"></div>
          <div>
            <div class="ae-stage-title"><span class="ae-stage-agent">{agent} &middot;</span> {title}<span class="ae-tag">Not built yet</span></div>
          </div>
        </li>""" for agent, title in PLANNED_STAGES]


def events(run_events: list[AgentEvent]) -> None:
    """The run's recorded activity log, in order, followed by the agents not built yet."""
    rows = []
    for event in run_events:
        timing = f" &middot; {event.duration_ms / 1000:.1f} s" if event.duration_ms and event.duration_ms >= 1000 else ""
        rows.append(f"""
        <li class="ae-stage">
          <div class="ae-dot {event.status}">{ICONS.get(event.status, "")}</div>
          <div>
            <div class="ae-stage-title"><span class="ae-stage-agent">{escape(event.agent)} &middot;</span> {escape(event.action)}</div>
            <div class="ae-stage-detail">{escape(event.detail)}{timing}</div>
          </div>
        </li>""")
    st.html(f'<ul class="ae-stages">{"".join(rows + _planned_rows())}</ul>')


def stages(run_stages: list[Stage]) -> None:
    """Workflow stages for runs saved before the activity log existed."""
    icons = ICONS
    rows = []
    for stage in run_stages:
        rows.append(f"""
        <li class="ae-stage">
          <div class="ae-dot {stage.status}">{icons.get(stage.status, "")}</div>
          <div>
            <div class="ae-stage-title"><span class="ae-stage-agent">{escape(stage.agent)} &middot;</span> {escape(stage.title)}</div>
            <div class="ae-stage-detail">{escape(stage.detail)}</div>
          </div>
        </li>""")
    st.html(f'<ul class="ae-stages">{"".join(rows + _planned_rows())}</ul>')


def handoff(planner_name: str, brief: TaskBrief | None, plan: Plan | None) -> None:
    """Manager -> Planner -> Plan diagram. Parts that did not run are dimmed."""
    handed_off = brief is not None
    planned = plan is not None
    result_text = f"{len(plan.steps)} steps" if planned else "No plan"
    st.html(f"""
    <div class="ae-flow">
      <div class="ae-node active"><div class="ae-node-role">Manager</div><div class="ae-node-name">ManagerAgent</div></div>
      <div class="ae-arrow{'' if handed_off else ' off'}"><span>&darr;</span><span>TaskBrief</span></div>
      <div class="ae-node{' active' if handed_off else ' off'}"><div class="ae-node-role">Planner</div><div class="ae-node-name">{escape(planner_name)}</div></div>
      <div class="ae-arrow{'' if planned else ' off'}"><span>&darr;</span><span>Plan</span></div>
      <div class="ae-node{' active' if planned else ' off'}"><div class="ae-node-role">Output</div><div class="ae-node-name">{result_text}</div></div>
    </div>
    """)


def feature_chips(features: list[str]) -> None:
    if not features:
        st.html('<span class="ae-chip">None detected: core steps only</span>')
        return
    chips = "".join(f'<span class="ae-chip accent">{escape(FEATURE_LABELS.get(f, f))}</span>' for f in features)
    st.html(f"<div>{chips}</div>")


def step_list(numbered_steps: list[tuple[int, object]], planner_name: str, show_phase: bool) -> None:
    """Render (number, PlanStep) pairs, each tagged with the rule that added it."""
    rows = []
    for number, step in numbered_steps:
        origin = step_origin(step, planner_name)
        if origin == "core":
            chip = '<span class="ae-chip">Core</span>'
        elif origin:
            chip = f'<span class="ae-chip accent">{escape(FEATURE_LABELS.get(origin, origin))}</span>'
        elif planner_name != RuleBasedPlanner.name:
            chip = '<span class="ae-chip">Local AI</span>'
        else:
            chip = ""
        phase = f'<span class="ae-step-phase">{escape(step.phase)}</span>' if show_phase else ""
        rows.append(f"""
        <li class="ae-step">
          <span class="ae-step-num">{number:02d}</span>
          <span>{phase}{escape(step.description)}</span>
          {chip}
        </li>""")
    st.html(f'<ul class="ae-steps">{"".join(rows)}</ul>')


def history_row(requirement: str, meta: str) -> None:
    st.html(f'<div class="ae-history-req">{escape(requirement)}</div><div class="ae-history-meta">{escape(meta)}</div>')
