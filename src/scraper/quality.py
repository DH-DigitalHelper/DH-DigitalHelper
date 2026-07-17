"""Moderate quality gate: decide whether an extracted doc joins the corpus."""

from __future__ import annotations

import re

# --- nav / link-list backstop -------------------------------------------------
# trafilatura (favor_recall=True, in html_extract.py) already strips most TYPO3
# nav/footer/menu boilerplate before the gate sees a doc, so this is a BACKSTOP
# for the survivors: pages that are still almost entirely a list of links. We
# measure the share of *anchor words* -- words inside a markdown link
# `[anchor](url)` -- against the doc's total words. Real prose keeps this low
# even with a few inline links; a bare link list runs near 1.0. This replaces
# the old "fraction of lines that START with a bullet AND contain a link" rule,
# which missed inline links and non-bulleted link bars entirely.
_NAV_LINK_RATIO = 0.6
# `(?<!!)` excludes images: an image is `![alt](url)`, whose `[alt](url)` tail
# matches a bare link pattern. Counting those alt words put images in the
# numerator while word_count (derived from `text`, which has images stripped)
# left them out of the denominator -- comparing two different documents, with a
# ratio that could even exceed 1.0, so an image-heavy page carrying real prose
# was rejected as nav-only.
_LINK_ANCHOR = re.compile(r"(?<!!)\[([^\]]*)\]\([^)]*\)")

# --- login / cookie / empty-state boilerplate ---------------------------------
# Curated high-signal marker phrases (German first, English second), matched
# case-insensitively as substrings on the PLAIN text. CONSERVATIVE by design: a
# doc is rejected only when it is BOTH short (< _BOILERPLATE_MAX_WORDS) AND
# carries at least _BOILERPLATE_MIN_HITS *distinct* phrases from ONE category.
# So a long page that merely mentions cookies/login/404 in passing is never even
# scanned, and a short page needs two independent signals from one category.
# Phrases within a category are kept non-overlapping (no phrase a substring of
# another) so the distinct-hit count is honest -- preserve that when extending.
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
    """Category whose distinct marker phrases dominate the doc, else None.
    Counts distinct phrase hits per category; the category with the most hits
    wins if it reaches _BOILERPLATE_MIN_HITS (ties resolved by _MARKERS order)."""
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

    # Login/cookie/empty-state boilerplate. Only scanned on SHORT docs, so a long
    # real page that mentions these terms in passing is never dropped -- and the
    # substring scan therefore runs on a minority of docs, staying out of the hot
    # path. Placed before the nav backstop so the more specific reason wins.
    if word_count < _BOILERPLATE_MAX_WORDS:
        cat = _dominant_boilerplate(text.lower())
        if cat is not None:
            return False, f"boilerplate/{cat}"

    # Nav / link-list backstop: reject when anchor words dominate the visible text.
    markdown = doc.get("markdown") or ""
    if _link_word_ratio(markdown, word_count) > _NAV_LINK_RATIO:
        return False, "boilerplate/nav-only"

    return True, "ok"
