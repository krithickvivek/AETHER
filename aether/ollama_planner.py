"""OllamaPlanner: asks a small language model running locally in Ollama for a plan.

Only talks to an Ollama server on this computer (localhost), so it costs nothing
and sends nothing to the internet. The model's answer is checked strictly; if it
is not good enough, create_plan raises PlannerError and the caller can fall back
to RuleBasedPlanner (see aether/fallback.py).
"""

import json
import math
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Callable
from urllib.parse import urlparse

from aether.manager import FEATURE_KEYWORDS
from aether.models import Plan, PlanStep, TaskBrief
from aether.planner import PHASES, Planner

DEFAULT_MODEL = "llama3.2:1b"
ALLOWED_MODELS = {DEFAULT_MODEL}  # the only model tested on this project's hardware
DEFAULT_URL = "http://127.0.0.1:11434"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
# A plan needs about 300-600 tokens. The cap stops a model that keeps repeating itself
# within a minute or two instead of running until the timeout.
MAX_OUTPUT_TOKENS = 900

# --- Quality rules. The prompt asks for tidy targets; validation only rejects plans that
# miss them by a clear margin, so a useful plan with a slightly different shape still passes.
MIN_WORDS = 4                 # shorter steps ("Create the design") are too vague to act on
MAX_PHASE_SHARE = 0.45        # no phase may hold more than 45% of the steps...
MIN_PHASE_CAP = 3             # ...but 3 steps in one phase are always allowed
MAX_DROPPED_SHARE = 0.25      # more unusable or repeated steps than this means a repetitive answer
LETTER_SIMILARITY = 0.85      # near-identical wording, in any phase
WORD_OVERLAP = 0.55           # same key words, in the same phase

# How much work each feature adds. The total decides the project's tier.
FEATURE_WEIGHTS = {"authentication": 2, "database": 2, "api": 2, "web_ui": 1}


@dataclass(frozen=True)
class Tier:
    name: str
    min_score: int
    min_steps: int
    max_steps: int
    min_phases: int


TIERS = [  # checked from the most complex down
    Tier("Complex", 5, 10, 30, 5),
    Tier("Moderate", 2, 8, 22, 5),
    Tier("Simple", 0, 6, 14, 4),
]

# What to ask for, and how to recognise it in the answer, for each detected feature.
FEATURE_HINTS = {
    "authentication": "user registration, login and logout",
    "database": "storing and loading data in a database",
    "web_ui": "the web pages and user interface",
    "api": "the API endpoints",
}
EXTRA_KEYWORDS = {
    "authentication": ["auth", "authenticate", "authentication", "sign-in", "signin", "credentials",
                       "logout", "registration", "session", "sessions"],
    "database": ["data model", "schema", "crud", "storage", "persistence", "saving", "storing", "loading"],
    "web_ui": ["ui", "user interface", "interface", "page", "pages", "form", "forms", "front-end", "frontend"],
    "api": ["rest", "http", "request", "requests", "response", "responses"],
}

# Verbs that mean "build it". A feature only counts as implemented when an Implementation
# step performs one of these actions on it; mentioning it in a design step is not enough.
BUILD_VERBS = {
    "implement", "build", "create", "develop", "add", "code", "write", "program", "connect",
    "integrate", "store", "save", "persist", "enable", "handle", "make", "wire", "expose",
    "set", "hook", "construct", "support", "provide", "render", "serve",
}

STOP_WORDS = set(
    "a an the and or to for of in on with is it its be are as that this by from can into all each "
    "any their them they so at if not no our we you your will should must using use".split()
)
# Words naming a distinct kind of work. Steps naming different ones are never duplicates,
# e.g. "security testing" and "performance testing".
QUALIFIERS = {
    "unit", "integration", "end", "e2e", "security", "performance", "load", "stress", "accessibility",
    "usability", "regression", "smoke", "manual", "automated", "user", "developer", "admin", "api",
    "installation", "deployment", "mobile", "browser", "backup", "error", "logging", "validation",
}

SYSTEM_PROMPT = "You are a software project planner. You write clear, practical, non-repetitive development plans."

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "phase": {"type": "string", "enum": PHASES},
                    "description": {"type": "string"},
                },
                "required": ["phase", "description"],
            },
        }
    },
    "required": ["steps"],
}


class PlannerError(Exception):
    """The planner could not produce an acceptable plan."""


def _post_json(url: str, payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def check_local_url(base_url: str) -> str:
    """Refuse anything that is not an Ollama server on this computer."""
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in LOCAL_HOSTS:
        raise ValueError(f"OllamaPlanner only connects to this computer (localhost), not {base_url!r}.")
    return base_url.rstrip("/")


def check_local_model(model: str) -> str:
    """Allow only the approved local model (never an Ollama cloud model)."""
    if model not in ALLOWED_MODELS:
        raise ValueError(f"Model {model!r} is not allowed. Allowed local models: {', '.join(sorted(ALLOWED_MODELS))}.")
    return model


class OllamaPlanner(Planner):
    """Asks a local Ollama model for a plan and validates the answer."""

    name = "OllamaPlanner"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_URL,
        timeout: float = 300,
        send: Callable[[str, dict, float], dict] = _post_json,
    ):
        self.model = check_local_model(model)
        self.base_url = check_local_url(base_url)
        self.timeout = timeout
        self.send = send  # replaced by a fake in tests, so tests never need Ollama
        self.last_stats: dict = {}

    def create_plan(self, brief: TaskBrief) -> Plan:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_prompt(brief)},
            ],
            "format": RESPONSE_SCHEMA,
            "stream": False,
            "keep_alive": 0,  # unload the model straight away to give memory back
            "options": {"num_ctx": 2048, "temperature": 0.2, "num_predict": MAX_OUTPUT_TOKENS},
        }
        try:
            data = self.send(f"{self.base_url}/api/chat", payload, self.timeout)
        except urllib.error.HTTPError as exc:
            raise PlannerError(f"Ollama returned HTTP {exc.code} (is the model {self.model!r} downloaded?)") from exc
        except TimeoutError as exc:
            raise PlannerError(self._timeout_message()) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise PlannerError(self._timeout_message()) from exc
            raise PlannerError(f"Could not reach Ollama at {self.base_url} (is it running?)") from exc
        except OSError as exc:
            raise PlannerError(f"Could not reach Ollama at {self.base_url} (is it running?)") from exc

        self.last_stats = {
            "model": self.model,
            "output_tokens": data.get("eval_count"),
            "seconds": round(data.get("total_duration", 0) / 1e9, 1),
        }
        if data.get("done_reason") == "length":
            raise PlannerError(f"The model's answer was cut off at {MAX_OUTPUT_TOKENS} tokens "
                               "(it was probably repeating itself).")
        content = (data.get("message") or {}).get("content", "")
        steps = parse_and_validate(content, brief)
        return Plan(requirement=brief.requirement, planner_name=self.name, steps=steps)

    def _timeout_message(self) -> str:
        limit = f"{self.timeout / 60:g} minutes" if self.timeout >= 60 else f"{self.timeout:g} seconds"
        return f"The local model took longer than {limit} to answer, so it was stopped."


def project_tier(features: list[str]) -> Tier:
    """Simple, Moderate or Complex, from how much work the detected features add."""
    score = sum(FEATURE_WEIGHTS.get(f, 1) for f in features)
    return next(tier for tier in TIERS if score >= tier.min_score)


def min_implementation_steps(tier: Tier, features: list[str]) -> int:
    if tier.name == "Complex":
        return len(features) + 1
    if tier.name == "Moderate":
        return max(2, len(features))
    return 1


def build_prompt(brief: TaskBrief) -> str:
    tier = project_tier(brief.features)
    impl = min_implementation_steps(tier, brief.features)
    lines = [f"Requirement: {brief.requirement}", ""]
    if brief.features:
        lines.append("Include one Implementation step that builds each of these:")
        lines += [f"- {FEATURE_HINTS.get(f, f)}" for f in brief.features]
        lines.append("")
    lines += [
        f"This is a {tier.name.lower()} project. Write a development plan with "
        f"{tier.min_steps + 1} to {(tier.min_steps + tier.max_steps) // 2} short, concrete steps.",
        f"Use only these phases, in this order: {', '.join(PHASES)}.",
        f"Suggested steps per phase: Requirements 1-2, Design 1-3, Setup 1-2, "
        f"Implementation {impl + 1}-{impl + 3}, Testing 2-3, Documentation 1-2.",
        "Start every Implementation step with an action such as Implement, Build, Create or Add.",
        "Every step must be different. Do not repeat or rephrase earlier steps.",
        "Reply with JSON only.",
    ]
    return "\n".join(lines)


def parse_and_validate(content: str, brief: TaskBrief) -> list[PlanStep]:
    """Turn the model's JSON into ordered PlanSteps, or raise PlannerError explaining why not."""
    try:
        raw_steps = json.loads(content)["steps"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise PlannerError("The model did not return the expected JSON.") from exc
    if not isinstance(raw_steps, list):
        raise PlannerError("The model did not return a list of steps.")

    steps: list[PlanStep] = []
    for item in raw_steps:
        step = _clean_step(item)
        if step is None or any(_duplicate(step, kept) for kept in steps):
            continue  # unusable, vague, repeated or reworded
        steps.append(step)
    if not steps:
        raise PlannerError("The model gave no usable steps.")

    tier = project_tier(brief.features)
    problems = quality_problems(steps, len(raw_steps) - len(steps), len(raw_steps), brief, tier)
    if problems:
        features = ", ".join(brief.features) or "no special features"
        raise PlannerError(f"{tier.name} project ({features}): {'; '.join(problems)}.")

    steps.sort(key=lambda step: PHASES.index(step.phase))
    return steps


def quality_problems(steps: list[PlanStep], dropped: int, given: int, brief: TaskBrief, tier: Tier) -> list[str]:
    """Every reason the plan is not good enough for this tier. Empty means it passes."""
    problems = []
    total = len(steps)
    per_phase = {phase: sum(1 for s in steps if s.phase == phase) for phase in PHASES}

    if not tier.min_steps <= total <= tier.max_steps:
        problems.append(f"the model gave {total} usable steps (expected {tier.min_steps}-{tier.max_steps})")
    if dropped / given > MAX_DROPPED_SHARE:
        problems.append(f"{dropped} of {given} steps were vague, unusable or repeated")

    used = sum(1 for count in per_phase.values() if count)
    if used < tier.min_phases:
        problems.append(f"the plan uses {used} phases (need at least {tier.min_phases})")
    if not per_phase["Requirements"] and not per_phase["Design"]:
        problems.append("no Requirements or Design step")
    for phase in ("Testing", "Documentation"):
        if not per_phase[phase]:
            problems.append(f"no {phase} step")

    implementation = per_phase["Implementation"]
    needed = min_implementation_steps(tier, brief.features)
    if implementation < needed:
        problems.append(f"too few implementation steps ({implementation}, need {needed})")
    cap = max(MIN_PHASE_CAP, math.ceil(MAX_PHASE_SHARE * total))
    problems += [f"{phase} has {count} of {total} steps (unbalanced)"
                 for phase, count in per_phase.items() if count > cap]
    support = per_phase["Testing"] + per_phase["Documentation"]
    if support > 2 * implementation + 2:
        problems.append(f"testing and documentation outweigh implementation ({support} vs {implementation})")

    missing, not_built, thin = [], [], []
    for feature in brief.features:
        mentions = [s for s in steps if _mentions_feature(feature, s)]
        if not mentions:
            missing.append(feature)
        elif not any(_builds_feature(feature, s) for s in mentions):
            not_built.append(feature)
        elif len(mentions) < 2:
            thin.append(feature)
    if missing:
        problems.append(f"the plan does not cover: {', '.join(missing)}")
    if not_built:
        problems.append(f"no implementation step builds: {', '.join(not_built)}")
    if thin:
        problems.append(f"only one step mentions: {', '.join(thin)}")
    return problems


def _clean_step(item) -> PlanStep | None:
    if not isinstance(item, dict) or item.get("phase") not in PHASES:
        return None
    description = " ".join(str(item.get("description", "")).split())
    if len(description.split()) < MIN_WORDS:
        return None
    return PlanStep(phase=item["phase"], description=description)


def _stem(word: str) -> str:
    """Rough word ending trim, so "tests", "testing" and "tested" all become "test"."""
    for suffix in ("ing", "ed", "s"):
        if word.endswith(suffix) and not word.endswith("ss") and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            break
    return word[:-1] if word.endswith("e") and len(word) > 3 else word


def _key_words(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP_WORDS}


QUALIFIER_STEMS = {_stem(q) for q in QUALIFIERS}
BUILD_VERB_STEMS = {_stem(v) for v in BUILD_VERBS}


def _duplicate(a: PlanStep, b: PlanStep) -> bool:
    words_a, words_b = _key_words(a.description), _key_words(b.description)
    if (words_a - words_b) & QUALIFIER_STEMS and (words_b - words_a) & QUALIFIER_STEMS:
        return False  # each names a different kind of work, e.g. security vs performance testing
    if SequenceMatcher(None, a.description.lower(), b.description.lower()).ratio() >= LETTER_SIMILARITY:
        return True
    if a.phase != b.phase or not words_a or not words_b:
        return False
    return len(words_a & words_b) / len(words_a | words_b) >= WORD_OVERLAP


def _mentions_feature(feature: str, step: PlanStep) -> bool:
    keywords = FEATURE_KEYWORDS.get(feature, []) + EXTRA_KEYWORDS.get(feature, [])
    text = step.description.lower()
    return any(re.search(rf"\b{re.escape(kw)}\b", text) for kw in keywords)


def _builds_feature(feature: str, step: PlanStep) -> bool:
    """True only for an Implementation step that performs a build action on the feature."""
    return (step.phase == "Implementation" and _mentions_feature(feature, step)
            and bool(_key_words(step.description) & BUILD_VERB_STEMS))


def ollama_status(model: str = DEFAULT_MODEL, base_url: str = DEFAULT_URL, timeout: float = 2) -> tuple[bool, str]:
    """Quick local check: is Ollama running and is the model downloaded? Never raises."""
    try:
        request = urllib.request.Request(f"{check_local_url(base_url)}/api/tags")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            names = {m.get("name") for m in json.loads(response.read()).get("models", [])}
    except (ValueError, urllib.error.URLError, OSError, json.JSONDecodeError):
        return False, "Ollama is not running on this computer."
    if model not in names:
        return False, f"Ollama is running, but the model {model} is not downloaded."
    return True, f"Ollama is running with {model}."
