"""Extract main content + metadata from HTML using trafilatura."""

from __future__ import annotations

from . import markdown as md

# Text is derived from the one markdown pass rather than a second extraction:
# trafilatura prunes its parse tree in place during extract(), so calling it
# again on the same tree is unsafe -- and this halves the per-document cost.
# The stripping itself lives in `markdown.to_text`, shared with the PDF path so
# both produce the same notion of "word" for the min_words gate.
_markdown_to_text = md.to_text


def extract_html(html: str | bytes, url: str | None = None) -> dict | None:
    """Extract main content + metadata. Accepts raw ``bytes`` (preferred: lets
    trafilatura detect the page's encoding) or an already-decoded ``str``."""
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
