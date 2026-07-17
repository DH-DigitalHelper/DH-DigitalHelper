"""Title helpers for documents whose extractor produced no title.

Two independent fallbacks, kept out of the extractors and storage so both the
forward write path and the one-time backfill share one definition:

* ``clean`` sanitises a PDF's *embedded* ``doc.metadata['title']`` and rejects the
  common junk (placeholders, Office "Microsoft Word - foo.docx" artifacts, a bare
  filename) -- for those the URL basename is a better title than the embedded one.
* ``from_url`` derives a readable title from the URL's last path segment.
"""

from __future__ import annotations

import os
import re
from urllib.parse import unquote, urlsplit

_PLACEHOLDERS = {"untitled", "unbenannt", "title", "dokument1", "document1"}
_TOOL_PREFIXES = ("microsoft word -", "microsoft powerpoint -", "microsoft excel -")
_BARE_FILENAME = re.compile(r"[\w .\-]+\.(pdf|docx?|pptx?|xlsx?)\Z", re.IGNORECASE)
_WS = re.compile(r"\s+")


def clean(raw: str | None) -> str | None:
    """Sanitise an embedded PDF metadata title; ``None`` for junk."""
    if not raw:
        return None
    title = raw.strip()
    if not title:
        return None
    low = title.lower()
    if low in _PLACEHOLDERS:
        return None
    if low.startswith(_TOOL_PREFIXES):
        return None
    if _BARE_FILENAME.match(low):
        return None
    return title


def from_url(url: str) -> str | None:
    """Readable title from a URL's last path segment, or ``None`` if there is none."""
    path = urlsplit(url).path
    if path.endswith("/"):
        return None
    base = unquote(path.rsplit("/", 1)[-1]) if path else ""
    base, _ext = os.path.splitext(base)
    base = _WS.sub(" ", base.replace("_", " ").replace("-", " ")).strip()
    return base or None
