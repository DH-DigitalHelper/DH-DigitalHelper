"""Extract main content + metadata from HTML using trafilatura."""

from __future__ import annotations

import re

# Applied in order to turn trafilatura markdown into plain text. trafilatura
# prunes its parse tree in place during extraction, so a second extract() call
# on the same tree is unsafe -- deriving text from the one markdown pass instead
# of running a second full extraction halves the per-document HTML cost.
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")  # images: drop entirely
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")  # links: keep the anchor text
_EMPHASIS = re.compile(r"(\*\*|\*|__|_|`)")  # bold/italic/inline-code markers
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)  # "### " prefixes
_BLOCKQUOTE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)  # "> " prefixes
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+", re.MULTILINE)  # bullets/numbers
_HRULE = re.compile(r"^\s*(?:[-*_]\s*){3,}$", re.MULTILINE)  # --- *** ___


def _markdown_to_text(markdown: str) -> str:
    """Strip markdown formatting to plain text for indexing / word counting.

    Only the *presentation* syntax is removed; the words themselves (headings,
    link anchors, table cells) are preserved so ``word_count`` and full-text
    search stay faithful to the content."""
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


def extract_html(html: str, url: str | None = None) -> dict | None:
    # Imported lazily so PDF-only pool workers never pay trafilatura's heavy
    # import cost (and vice versa for pymupdf in pdf_extract).
    import trafilatura
    from trafilatura.metadata import extract_metadata

    markdown = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_comments=False,
        include_tables=True,
        favor_recall=True,
    )
    if not markdown:
        return None

    text = _markdown_to_text(markdown)

    meta = extract_metadata(html, default_url=url)
    return {
        "title": getattr(meta, "title", None) if meta else None,
        "text": text,
        "markdown": markdown,
        "lang": None,
        "word_count": len(text.split()),
        "metadata": {
            "author": getattr(meta, "author", None) if meta else None,
            "date": getattr(meta, "date", None) if meta else None,
            "description": getattr(meta, "description", None) if meta else None,
            "sitename": getattr(meta, "sitename", None) if meta else None,
        },
    }
