"""Deterministic language identification (ISO-639-1) for extracted document text.

Shared by both extractor paths and the backfill so HTML and PDF get one, identical
notion of "language" -- mirroring how ``markdown.to_text`` gives them one notion of
"word". Storing ``None`` on thin or ambiguous text beats storing a wrong guess:
downstream can treat NULL as "unknown".
"""

from __future__ import annotations

# Below this many characters there is not enough signal to trust a guess.
_MIN_CHARS = 20
# py3langid returns a normalized probability in [0, 1] with norm_probs=True; below
# this the top language is a coin-flip, so we decline to label rather than mislabel.
_MIN_CONFIDENCE = 0.5

_identifier = None


def _get_identifier():
    """Lazily build the one shared identifier (deferred so importing this module
    stays cheap and PDF-only pool workers do not pay for it until first use)."""
    global _identifier
    if _identifier is None:
        from py3langid.langid import MODEL_FILE, LanguageIdentifier

        _identifier = LanguageIdentifier.from_pickled_model(MODEL_FILE, norm_probs=True)
    return _identifier


def detect(text: str | None) -> str | None:
    """Best-effort ISO-639-1 code for ``text`` (lowercased), or ``None`` when it is
    empty/too short or detection is below the confidence floor. Deterministic."""
    if not text:
        return None
    stripped = text.strip()
    if len(stripped) < _MIN_CHARS:
        return None
    code, prob = _get_identifier().classify(stripped)
    if prob < _MIN_CONFIDENCE:
        return None
    return code.lower()
