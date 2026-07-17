"""Turn extractor markdown into plain text for indexing and word counting.

Shared by BOTH extractor paths on purpose. ``word_count`` -- and therefore the
``min_words`` quality gate that reads it -- has to mean the same thing for an
HTML page and a PDF. When the PDF path used its raw markdown as ``text``,
``len(text.split())`` counted ``#``, ``|``, ``---``, ``-`` and ``>`` as words
while the HTML path stripped them first, so one gate was being applied to two
different notions of "word".

Only the *presentation* syntax is removed; the words themselves (headings, link
anchors, table cells) are preserved so ``word_count`` and full-text search stay
faithful to the content.
"""

from __future__ import annotations

import re

# Applied in order.
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")  # images: drop entirely
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")  # links: keep the anchor text
_EMPHASIS = re.compile(r"(\*\*|\*|__|_|`)")  # bold/italic/inline-code markers
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)  # "### " prefixes
_BLOCKQUOTE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)  # "> " prefixes
# Bullets only. A leading "N." is deliberately NOT stripped: `_HEADING` runs
# first, so "## 1. Semester" reaches this rule as a line starting "1. " and is
# indistinguishable from an ordered-list item -- the ordinal was being deleted,
# indexing the heading as bare "Semester". For DHBW module/semester content that
# ordinal IS the meaning ("1. Semester" vs "2. Semester" both became "Semester"),
# and an ordered-list number is legitimate content anyway, so keeping it costs a
# faithful word or two and buys back a searchable distinction.
_LIST_MARKER = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
# Horizontal rules AND markdown table separator rows (`| --- | :--- |`): without
# the optional pipes these rows survived, and `|` -> space then left a run of
# bare `---` tokens that split() counts as words.
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
    text = text.replace("|", " ")  # table cell separators -> whitespace
    # Collapse runs of blank lines but keep paragraph breaks readable.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
