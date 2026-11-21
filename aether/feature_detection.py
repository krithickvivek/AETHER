"""Feature detection: which software features a requirement (or a plan step) talks about.

This module has no dependency on the Manager, the planners or the templates, so all of them can
use it without importing each other. It depends only on aether.models.

Two vocabularies are used, deliberately kept apart:
  * FEATURE_KEYWORDS: words in a user's *requirement* that mean a feature is wanted
    (used by detect_features, which the Manager runs).
  * EXTRA_KEYWORDS: additional words that show a *plan step* is about a feature
    (used together with FEATURE_KEYWORDS by mentions_feature, for checking AI plans and
    mapping steps to template files). They are never used to detect features in a requirement.

The detected features are currently the four software-engineering ones below. Other kinds of
feature (for example machine learning) are not detected here yet.
"""

import re

from aether.models import PlanStep

# Keywords that tell the Manager a requirement needs a particular feature.
FEATURE_KEYWORDS = {
    "authentication": [
        "login", "log in", "logs in", "sign in", "sign up", "signup",
        "register", "password", "account", "accounts", "authentication",
    ],
    "database": [
        "database", "db", "save", "saves", "saved", "store", "stores", "stored",
        "persist", "record", "records",
    ],
    "web_ui": [
        "website", "web", "webpage", "web page", "frontend", "browser", "dashboard",
    ],
    "api": ["api", "apis", "endpoint", "endpoints"],
}

# Extra words that show a plan step is about a feature (see mentions_feature).
EXTRA_KEYWORDS = {
    "authentication": ["auth", "authenticate", "authentication", "sign-in", "signin", "credentials",
                       "logout", "registration", "session", "sessions"],
    "database": ["data model", "schema", "crud", "storage", "persistence", "saving", "storing", "loading"],
    "web_ui": ["ui", "user interface", "interface", "page", "pages", "form", "forms", "front-end", "frontend"],
    "api": ["rest", "http", "request", "requests", "response", "responses"],
}

# Every detectable feature, in the order they are reported.
FEATURES = tuple(FEATURE_KEYWORDS)


def _contains_word(keyword: str, lowered_text: str) -> bool:
    return re.search(rf"\b{re.escape(keyword)}\b", lowered_text) is not None


def detect_features(text: str) -> list[str]:
    """Return the features whose keywords appear in the text (as whole words)."""
    lowered = text.lower()
    return [feature for feature, keywords in FEATURE_KEYWORDS.items()
            if any(_contains_word(keyword, lowered) for keyword in keywords)]


def mentions_feature(feature: str, step: PlanStep) -> bool:
    """True if the plan step's description uses a word that belongs to the feature."""
    keywords = FEATURE_KEYWORDS.get(feature, []) + EXTRA_KEYWORDS.get(feature, [])
    text = step.description.lower()
    return any(_contains_word(keyword, text) for keyword in keywords)
