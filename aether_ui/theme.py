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

# Design tokens. Dark is the primary look; light mode keeps working with its own palette.
# accent: fills (buttons, marks); accent_text: accent-coloured text, tuned for contrast on the background.
LIGHT = {
    "accent": "#6D28D9", "accent_text": "#6D28D9", "accent_soft": "rgba(109, 40, 217, 0.07)",
    "accent_line": "rgba(109, 40, 217, 0.35)", "glow": "rgba(109, 40, 217, 0.10)",
    "surface": "#FAFAFB", "surface_2": "#F4F4F6", "border": "#E4E4E7", "text": "#18181B", "muted": "#71717A",
    "ok": "#16A34A", "warn": "#B45309", "fail": "#DC2626", "on_status": "#FFFFFF",
}
DARK = {
    "accent": "#8B5CF6", "accent_text": "#A78BFA", "accent_soft": "rgba(139, 92, 246, 0.10)",
    "accent_line": "rgba(167, 139, 250, 0.40)", "glow": "rgba(139, 92, 246, 0.16)",
    "surface": "#111114", "surface_2": "#17171B", "border": "#26262B", "text": "#EDEDEF", "muted": "#8B8B94",
    "ok": "#34D399", "warn": "#FBBF24", "fail": "#F87171", "on_status": "#09090B",
}

# The agents that exist today, shown as available in the hero.
AVAILABLE_AGENTS = ["Manager", "Planner", "Developer"]

# Agents planned for later stages of the project. Shown as "Not built yet".
PLANNED_STAGES = [
    ("Reviewer", "Review code"),
    ("Tester", "Write and run tests"),
    ("Documentation", "Write project documentation"),
]

CSS = Template("""
<style>
:root {
  --ae-accent: $accent; --ae-accent-text: $accent_text; --ae-accent-soft: $accent_soft;
  --ae-accent-line: $accent_line; --ae-glow: $glow;
  --ae-surface: $surface; --ae-surface-2: $surface_2; --ae-border: $border; --ae-text: $text; --ae-muted: $muted;
  --ae-ok: $ok; --ae-warn: $warn; --ae-fail: $fail; --ae-on-status: $on_status;
  --ae-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
}
.block-container, [data-testid="stMainBlockContainer"] { max-width: 1200px; padding-top: 4.5rem; padding-bottom: 4rem; }
h1, h2, h3 { letter-spacing: -0.015em; }
.ae-mono { font-family: var(--ae-mono); }

.ae-bar { display: flex; align-items: center; justify-content: space-between; gap: 1rem;
  flex-wrap: wrap; padding-bottom: 1rem; border-bottom: 1px solid var(--ae-border); margin-bottom: 0.5rem; }
.ae-brand { display: flex; align-items: center; gap: 0.75rem; min-width: 0; }
.ae-mark { width: 32px; height: 32px; border-radius: 8px; color: #fff; flex: none;
  background: linear-gradient(135deg, var(--ae-accent), var(--ae-accent-text));
  box-shadow: 0 0 0 1px var(--ae-accent-line), 0 4px 16px var(--ae-glow);
  display: grid; place-items: center; font-weight: 700; font-size: 0.95rem; }
.ae-name { font-size: 1.05rem; font-weight: 700; letter-spacing: 0.18em; line-height: 1.1; }
.ae-expansion { color: var(--ae-muted); font-size: 0.78rem; line-height: 1.3; }
.ae-badge { display: inline-flex; align-items: center; gap: 0.45rem; padding: 0.22rem 0.7rem;
  border: 1px solid var(--ae-border); border-radius: 999px; font-size: 0.72rem; color: var(--ae-muted);
  background: var(--ae-surface); font-family: var(--ae-mono); white-space: nowrap; }
.ae-badge::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: var(--ae-ok); }

.ae-hero { position: relative; padding: 2.25rem 0 1.25rem; }
.ae-hero::before { content: ""; position: absolute; inset: -1rem -2rem auto -2rem; height: 15rem; z-index: -1;
  background: radial-gradient(ellipse 60% 70% at 15% 0%, var(--ae-glow), transparent 70%); pointer-events: none; }
.ae-eyebrow { font-family: var(--ae-mono); font-size: 0.72rem; letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--ae-accent-text); margin-bottom: 0.75rem; }
.ae-tagline { font-size: clamp(1.75rem, 4vw, 2.75rem); font-weight: 680; letter-spacing: -0.03em;
  line-height: 1.08; margin: 0 0 0.75rem; max-width: 22ch; }
.ae-lede { color: var(--ae-muted); font-size: 1rem; line-height: 1.55; max-width: 44rem; margin: 0 0 1.25rem; }
.ae-roster { display: flex; flex-wrap: wrap; align-items: center; gap: 0.45rem; }
.ae-agent { display: inline-flex; align-items: center; gap: 0.4rem; padding: 0.22rem 0.65rem; border-radius: 999px;
  font-size: 0.78rem; border: 1px solid var(--ae-border); color: var(--ae-muted); white-space: nowrap; }
.ae-agent::before { content: ""; width: 7px; height: 7px; border-radius: 50%; border: 1.5px dashed var(--ae-muted); box-sizing: border-box; }
.ae-agent.live { color: var(--ae-text); border-color: var(--ae-accent-line); background: var(--ae-accent-soft); }
.ae-agent.live::before { background: var(--ae-ok); border: none; }
.ae-roster-note { color: var(--ae-muted); font-size: 0.75rem; margin-left: 0.35rem; }
.ae-hero.compact { padding: 1.25rem 0 0.5rem; }
.ae-hero.compact::before { height: 8rem; }
.ae-hero.compact .ae-tagline { font-size: clamp(1.2rem, 2.4vw, 1.5rem); max-width: none; margin-bottom: 0.6rem; }
.ae-hero.compact .ae-eyebrow, .ae-hero.compact .ae-lede { display: none; }

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
.ae-dot.done { background: var(--ae-ok); border-color: var(--ae-ok); color: var(--ae-on-status); }
.ae-dot.failed { background: var(--ae-fail); border-color: var(--ae-fail); color: var(--ae-on-status); }
.ae-dot.planned { border-style: dashed; }
.ae-dot.waiting { border-color: var(--ae-accent-text); color: var(--ae-accent-text); }
.ae-stage-title { font-weight: 600; font-size: 0.9rem; }
.ae-stage-agent { color: var(--ae-muted); font-weight: 500; }
.ae-stage-detail { color: var(--ae-muted); font-size: 0.8rem; }
.ae-stage.planned .ae-stage-title { color: var(--ae-muted); font-weight: 500; }
.ae-tag { font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--ae-muted);
  border: 1px solid var(--ae-border); border-radius: 4px; padding: 0 0.3rem; margin-left: 0.35rem; }

.ae-flow { display: flex; flex-direction: column; gap: 0.2rem; margin-bottom: 0.75rem; }
.ae-node { border: 1px solid var(--ae-border); border-radius: 10px; padding: 0.5rem 0.75rem;
  display: flex; justify-content: space-between; align-items: baseline; gap: 0.75rem; }
.ae-node.active { border-color: var(--ae-accent-line); background: var(--ae-accent-soft); }
.ae-node.off { opacity: 0.45; }
.ae-node-role { font-size: 0.66rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--ae-muted); }
.ae-node-name { font-weight: 600; font-size: 0.86rem; }
.ae-arrow { display: flex; align-items: center; gap: 0.5rem; color: var(--ae-accent-text);
  font-size: 0.72rem; font-family: var(--ae-mono); padding: 0.1rem 0 0.1rem 1rem; }
.ae-arrow.off { color: var(--ae-muted); opacity: 0.6; }

.ae-chip { display: inline-block; padding: 0.12rem 0.55rem; border-radius: 999px; font-size: 0.76rem;
  border: 1px solid var(--ae-border); color: var(--ae-muted); margin: 0 0.35rem 0.35rem 0; white-space: nowrap; }
.ae-chip.accent { border-color: var(--ae-accent-line); color: var(--ae-accent-text); background: var(--ae-accent-soft); }

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
.ae-empty-steps b { color: var(--ae-accent-text); margin-right: 0.3rem; }

/* Requirement input panel: the keyed container in app.py (st.container(key="ae_input")). */
.st-key-ae_input { border: 1px solid var(--ae-border); border-radius: 14px; background: var(--ae-surface);
  padding: 1rem 1.15rem 1.15rem; margin: 0.5rem 0 0; transition: border-color 0.15s, box-shadow 0.15s; }
.st-key-ae_input:focus-within { border-color: var(--ae-accent-line); box-shadow: 0 0 0 3px var(--ae-glow); }
.st-key-ae_input textarea { font-size: 0.98rem; line-height: 1.55; }
.ae-input-head { display: flex; justify-content: space-between; align-items: baseline; gap: 0.25rem 1rem; flex-wrap: wrap; }
.ae-input-head .ae-label { margin: 0; }
.ae-input-hint { color: var(--ae-muted); font-size: 0.78rem; }
.st-key-generate button { min-height: 2.75rem; font-weight: 600; box-shadow: 0 6px 20px var(--ae-glow); }

/* Checkpoint A panel: the keyed container in app.py (st.container(key="ae_checkpoint")). */
.st-key-ae_checkpoint { border: 1px solid var(--ae-accent-line); border-radius: 14px; padding: 1.1rem 1.25rem 1.2rem;
  margin: 0.5rem 0 0.75rem; background: linear-gradient(180deg, var(--ae-accent-soft), var(--ae-surface) 9rem);
  box-shadow: 0 0 0 3px var(--ae-glow); }
.ae-cp-head { display: flex; justify-content: space-between; align-items: center; gap: 0.5rem 1rem; flex-wrap: wrap; }
.ae-cp-title { font-size: 1.05rem; font-weight: 650; letter-spacing: -0.01em; }
.ae-cp-badges { display: flex; flex-wrap: wrap; gap: 0.4rem; }
.ae-status { display: inline-flex; align-items: center; gap: 0.4rem; padding: 0.18rem 0.6rem; border-radius: 999px;
  font-size: 0.74rem; font-weight: 600; border: 1px solid var(--ae-border); color: var(--ae-muted); white-space: nowrap; }
.ae-status::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.ae-status.ok { color: var(--ae-ok); border-color: currentColor; }
.ae-status.warn { color: var(--ae-warn); border-color: currentColor; }
.ae-status.fail { color: var(--ae-fail); border-color: currentColor; }
.ae-status.neutral { font-weight: 500; font-family: var(--ae-mono); }
.ae-status.neutral::before { display: none; }
.st-key-approve_plan button { min-height: 2.75rem; font-weight: 600; }
.st-key-reject_plan button { min-height: 2.75rem; border-color: var(--ae-fail); color: var(--ae-fail); }
.st-key-reject_plan button:hover:not(:disabled) { background: color-mix(in srgb, var(--ae-fail) 12%, transparent);
  border-color: var(--ae-fail); color: var(--ae-fail); }

/* Generated files panel: the keyed container in app.py (st.container(key="ae_files")). */
.st-key-ae_files { border: 1px solid var(--ae-border); border-radius: 14px; background: var(--ae-surface);
  padding: 1.1rem 1.25rem 1.2rem; margin: 0.5rem 0 0.75rem; }
.ae-file-head { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 0.35rem 1rem; align-items: center;
  padding: 0.75rem 0.9rem; border: 1px solid var(--ae-border); border-radius: 10px; background: var(--ae-surface-2); }
.ae-file-name { font-weight: 650; font-size: 0.98rem; overflow-wrap: anywhere; }
.ae-file-path { font-family: var(--ae-mono); font-size: 0.76rem; color: var(--ae-muted); overflow-wrap: anywhere; }
.ae-file-tags { display: flex; flex-wrap: wrap; gap: 0.35rem; justify-content: flex-end; }
.ae-file-meta { grid-column: 1 / -1; font-size: 0.76rem; color: var(--ae-muted); }

.ae-progress { list-style: none; display: grid; grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0.5rem; padding: 0 !important; margin: 0.25rem 0 1rem !important; }
.ae-progress li { position: relative; display: flex; align-items: center; gap: 0.6rem; min-width: 0; margin: 0 !important;
  padding: 0.55rem 0.75rem; border: 1px solid var(--ae-border); border-radius: 10px; background: var(--ae-surface); }
.ae-progress li + li::before { content: ""; position: absolute; left: -0.5rem; top: 50%; width: 0.5rem;
  border-top: 1px solid var(--ae-border); }
.ae-progress .ae-dot { margin-top: 0; }
.ae-progress-text { min-width: 0; }
.ae-progress-title { font-size: 0.84rem; font-weight: 600; line-height: 1.2; }
.ae-progress-detail { font-size: 0.74rem; color: var(--ae-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ae-progress li.pending .ae-progress-title { color: var(--ae-muted); font-weight: 500; }
.ae-progress li.done + li::before { border-top-color: var(--ae-ok); }
.ae-progress li.current { border-color: var(--ae-accent-line); background: var(--ae-accent-soft);
  box-shadow: 0 0 0 3px var(--ae-glow); }
.ae-progress li.current .ae-dot { background: var(--ae-accent); border-color: var(--ae-accent); color: #fff; }
.ae-progress li.current .ae-progress-detail { color: var(--ae-accent-text); }
.ae-progress li.failed { border-color: var(--ae-fail); }

.ae-history-req { font-weight: 600; font-size: 0.92rem; overflow-wrap: anywhere; }
.ae-history-meta { color: var(--ae-muted); font-size: 0.8rem; margin-top: 0.2rem; }

@media (max-width: 640px) {
  .ae-step { grid-template-columns: 1.6rem 1fr; }
  .ae-step .ae-chip { grid-column: 2; justify-self: start; }
  .ae-expansion { display: none; }
  .block-container, [data-testid="stMainBlockContainer"] { padding-left: 1rem; padding-right: 1rem; }
  .ae-hero { padding-top: 1.5rem; }
  .ae-hero::before { inset: -1rem -1rem auto -1rem; }
  .ae-lede { font-size: 0.94rem; }
  .ae-roster-note { flex-basis: 100%; margin: 0.2rem 0 0; }
  .st-key-ae_input { padding: 0.85rem 0.85rem 0.95rem; border-radius: 12px; }
  .st-key-ae_checkpoint, .st-key-ae_files { padding: 0.9rem 0.85rem 1rem; border-radius: 12px; }
  .ae-file-head { grid-template-columns: 1fr; }
  .ae-file-tags { justify-content: flex-start; }
  .ae-progress { gap: 0.35rem; }
  .ae-progress li { flex-direction: column; text-align: center; gap: 0.3rem; padding: 0.5rem 0.25rem; }
  .ae-progress li + li::before { left: -0.35rem; width: 0.35rem; }
  .ae-progress .ae-dot { flex: none; width: 22px; height: 22px; }
  /* The four run metrics: two per row instead of four stacked rows. */
  [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] [data-testid="stMetric"]) > [data-testid="stColumn"] {
    min-width: calc(50% - 0.5rem) !important; flex: 1 1 calc(50% - 0.5rem) !important; }
  .ae-progress-title { font-size: 0.72rem; }
  .ae-progress-detail { display: none; }
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
      <span class="ae-badge">Runs locally &middot; no API keys</span>
    </div>
    """)


HERO_TAGLINE = "From requirement to a reviewed project scaffold."


def agent_roster() -> str:
    """Chips for the agents that exist today and the ones that are not built yet."""
    live = "".join(f'<span class="ae-agent live">{escape(name)}</span>' for name in AVAILABLE_AGENTS)
    planned = "".join(f'<span class="ae-agent">{escape(name)}</span>' for name, _ in PLANNED_STAGES)
    names = ", ".join(name for name, _ in PLANNED_STAGES[:-1]) + f" and {PLANNED_STAGES[-1][0]}"
    return (f'<div class="ae-roster">{live}{planned}'
            f'<span class="ae-roster-note">{escape(names)} are not built yet.</span></div>')


def hero(compact: bool = False) -> None:
    """The workspace header. Compact once a run is on screen, so the results sit higher."""
    st.html(f"""
    <div class="ae-hero{' compact' if compact else ''}">
      <div class="ae-eyebrow">Multi-agent engineering workspace</div>
      <div class="ae-tagline">{escape(HERO_TAGLINE)}</div>
      <p class="ae-lede">Describe the software you want. The Manager and Planner turn it into a phased plan,
        you review and approve it at a checkpoint, and only then does the Developer write a tested project
        template into its own folder. Generated code is never run.</p>
      {agent_roster()}
    </div>
    """)


def input_header(title: str, hint: str) -> None:
    st.html(f'<div class="ae-input-head"><div class="ae-label">{escape(title)}</div>'
            f'<div class="ae-input-hint">{escape(hint)}</div></div>')


def checkpoint_header(title: str, badges: list[tuple[str, str]]) -> None:
    """Checkpoint title with status badges; each badge is (text, tone) with tone ok, warn, fail or neutral."""
    chips = "".join(f'<span class="ae-status {escape(tone)}">{escape(text)}</span>' for text, tone in badges)
    st.html(f'<div class="ae-cp-head"><div class="ae-cp-title">{escape(title)}</div>'
            f'<div class="ae-cp-badges">{chips}</div></div>')


def file_header(record, language: str | None, changed: bool) -> None:
    """Name, folder, size and provenance of the file shown in the code viewer (a context.FileRecord)."""
    folder, _, name = record.path.rpartition("/")
    covers = ", ".join(map(str, record.covers)) or "none"
    tags = f'<span class="ae-status neutral">{escape(language or "text")}</span>'
    if changed:
        tags += '<span class="ae-status warn">Changed on disk</span>'
    st.html(f"""
    <div class="ae-file-head">
      <div class="ae-file-main">
        <div class="ae-file-name">{escape(name)}</div>
        <div class="ae-file-path">{escape(folder + '/' if folder else './')}{escape(name)}</div>
      </div>
      <div class="ae-file-tags"><span class="ae-status neutral">{record.size:,} bytes</span>{tags}</div>
      <div class="ae-file-meta">Written by {escape(record.author)} &middot; carries out plan steps {escape(covers)}
        &middot; SHA-256 <span class="ae-mono">{escape(record.sha256[:16])}&hellip;</span></div>
    </div>""")


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


def progress(steps: list) -> None:
    """The four-stage workflow indicator. Steps come from workflow.progress_steps (real run state)."""
    rows = []
    for number, step in enumerate(steps, start=1):
        mark = ICONS.get(step.status) if step.status in ("done", "failed") else str(number)
        current = ' aria-current="step"' if step.status == "current" else ""
        detail = f'<div class="ae-progress-detail">{escape(step.detail)}</div>' if step.detail else ""
        rows.append(f"""
        <li class="{escape(step.status)}"{current}>
          <div class="ae-dot {escape(step.status)}">{mark}</div>
          <div class="ae-progress-text"><div class="ae-progress-title">{escape(step.title)}</div>{detail}</div>
        </li>""")
    st.html(f'<ol class="ae-progress" aria-label="Workflow progress">{"".join(rows)}</ol>')


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
