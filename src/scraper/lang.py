"""Deterministic language identification (ISO-639-1) for extracted document text."""

from __future__ import annotations

_MIN_CHARS = 20
_MAX_CHARS = 10_000
_MIN_CONFIDENCE = 0.5

_identifier = None


def _get_identifier():
    """Lazily build the one shared identifier, deferred so importing this module stays cheap and PDF-only pool workers do not pay for it until first use."""
    global _identifier
    if _identifier is None:
        from py3langid.langid import MODEL_FILE, LanguageIdentifier

        _identifier = LanguageIdentifier.from_pickled_model(MODEL_FILE, norm_probs=True)
    return _identifier


def detect(text: str | None) -> str | None:
    """Best-effort ISO-639-1 code for text (lowercased), or None when it is empty, too short, or below the confidence floor."""
    if not text:
        return None
    stripped = text.strip()
    if len(stripped) < _MIN_CHARS:
        return None
    try:
        code, prob = _get_identifier().classify(stripped[:_MAX_CHARS])
    except Exception:
        return None
    if prob < _MIN_CONFIDENCE:
        return None
    return code.lower()
