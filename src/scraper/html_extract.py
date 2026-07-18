"""Extract main content + metadata from HTML using trafilatura."""

from __future__ import annotations

from . import lang as langmod
from . import markdown as md

_markdown_to_text = md.to_text


def extract_html(html: str | bytes, url: str | None = None) -> dict | None:
    """Extract main content + metadata."""
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
        "lang": langmod.detect(text),
        "word_count": len(text.split()),
        "metadata": {
            "author": getattr(meta, "author", None) if meta else None,
            "date": getattr(meta, "date", None) if meta else None,
            "description": getattr(meta, "description", None) if meta else None,
            "sitename": getattr(meta, "sitename", None) if meta else None,
        },
    }
