"""Moderate quality gate: decide whether an extracted doc joins the corpus."""

from __future__ import annotations

import re

_NAV_LINK_RATIO = 0.6
_LINK_ANCHOR = re.compile(r"(?<!!)\[([^\]]*)\]\([^)]*\)")

_BOILERPLATE_MAX_WORDS = 120
_BOILERPLATE_MIN_HITS = 2

_MARKERS: dict[str, tuple[str, ...]] = {
    "cookie": (
        "wir verwenden cookies",
        "diese website verwendet cookies",
        "diese webseite verwendet cookies",
        "verwendung von cookies",
        "notwendige cookies",
        "cookie-einstellungen",
        "alle akzeptieren",
        "datenschutzeinstellungen",
        "we use cookies",
        "this website uses cookies",
        "necessary cookies",
        "accept all cookies",
        "cookie settings",
    ),
    "login": (
        "bitte melden sie sich an",
        "angemeldet bleiben",
        "passwort vergessen",
        "zur anmeldung",
        "benutzername und passwort",
        "please log in",
        "please sign in",
        "keep me logged in",
        "forgot your password",
    ),
    "empty-state": (
        "seite nicht gefunden",
        "diese seite existiert nicht",
        "die angeforderte seite",
        "wurde nicht gefunden",
        "es wurden keine einträge gefunden",
        "keine ergebnisse gefunden",
        "keine treffer",
        "fehler 404",
        "error 404",
        "page not found",
        "no results found",
        "no entries found",
    ),
}


def _dominant_boilerplate(text_lower: str) -> str | None:
    """Category whose distinct marker phrases dominate the doc, else None."""
    best_cat: str | None = None
    best_hits = 0
    for cat, phrases in _MARKERS.items():
        hits = sum(1 for p in phrases if p in text_lower)
        if hits > best_hits:
            best_hits, best_cat = hits, cat
    return best_cat if best_hits >= _BOILERPLATE_MIN_HITS else None


def _link_word_ratio(markdown: str, word_count: int) -> float:
    """Fraction of the doc's words that live inside markdown link anchors."""
    if word_count <= 0:
        return 0.0
    link_words = sum(len(m.group(1).split()) for m in _LINK_ANCHOR.finditer(markdown))
    return link_words / word_count


def evaluate(doc, min_words: int = 50) -> tuple[bool, str]:
    if not doc:
        return False, "empty"
    text = (doc.get("text") or "").strip()
    if not text:
        return False, "empty"

    word_count = len(text.split())
    if word_count < min_words:
        return False, f"too short: {word_count} words"

    if word_count < _BOILERPLATE_MAX_WORDS:
        cat = _dominant_boilerplate(text.lower())
        if cat is not None:
            return False, f"boilerplate/{cat}"

    markdown = doc.get("markdown") or ""
    if _link_word_ratio(markdown, word_count) > _NAV_LINK_RATIO:
        return False, "boilerplate/nav-only"

    return True, "ok"
