"""Extract structured text from PDF bytes using PyMuPDF4LLM."""

from __future__ import annotations

from . import lang as langmod
from . import markdown as md
from . import pdf_title

_layout_disabled = False


def _to_markdown(data: bytes) -> str:
    global _layout_disabled
    import pymupdf
    import pymupdf4llm

    if not _layout_disabled:
        pymupdf4llm.use_layout(False)
        _layout_disabled = True

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        if doc.needs_pass:
            doc.authenticate("")
        return pymupdf4llm.to_markdown(doc, table_strategy=None)


def _meta_title(data: bytes) -> str | None:
    """The PDF's own embedded title (doc.metadata['title']), raw."""
    import pymupdf

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        if doc.needs_pass:
            doc.authenticate("")
        return doc.metadata.get("title")


def _heading_title(markdown: str) -> str | None:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def extract_pdf(data: bytes, to_markdown=None, meta_title=None) -> dict | None:
    if to_markdown is None:
        to_markdown = _to_markdown
    if meta_title is None:
        meta_title = _meta_title

    markdown = (to_markdown(data) or "").strip()
    if not markdown:
        return None

    text = md.to_text(markdown)

    title = _heading_title(markdown)
    if not title:
        title = pdf_title.clean(meta_title(data))

    return {
        "title": title,
        "text": text,
        "markdown": markdown,
        "lang": langmod.detect(text),
        "word_count": len(text.split()),
        "metadata": {"extractor": "pymupdf4llm"},
    }
