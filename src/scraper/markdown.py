"""Turn extractor markdown into plain text for indexing and word counting."""

from __future__ import annotations

import re

_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_EMPHASIS = re.compile(r"(\*\*|\*|__|_|`)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_BLOCKQUOTE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_LIST_MARKER = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_HRULE = re.compile(r"^\s*\|?(?:\s*[-*_:]{3,}\s*\|?)+\s*$", re.MULTILINE)


def to_text(markdown: str) -> str:
    """Strip markdown formatting to plain text for indexing / word counting."""
    text = markdown
    text = _HRULE.sub("", text)
    text = _IMAGE.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _HEADING.sub("", text)
    text = _BLOCKQUOTE.sub("", text)
    text = _LIST_MARKER.sub("", text)
    text = _EMPHASIS.sub("", text)
    text = text.replace("|", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
