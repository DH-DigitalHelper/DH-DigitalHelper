"""Extract structured text from PDF bytes using PyMuPDF4LLM.

PyMuPDF4LLM converts a PDF to Markdown with no ML models and no torch. It reads
directly from an in-memory stream, so no temp file is needed, and it is
stateless/thread-safe, so no per-worker converter object is required. The
``to_markdown`` seam stays injectable so tests run offline without loading the
real library.

Two settings keep this fast on the corpus of DHBW regulation/handbook PDFs
(prose-dense, born-digital, tens of thousands of words each):

* ``use_layout(False)`` -- pymupdf4llm >= 1.26 defaults to a "layout" engine that
  runs full document layout analysis *and OCR* (rendering every page at 300 DPI).
  That is 1.5-3x slower per document with no quality gain here and pulls in the
  slow/fragile OCR path. We force the classic text-based markdown converter --
  the "no ML models, no torch" path this module was written for.
* ``table_strategy=None`` -- per-page table detection (``page.find_tables``) is
  the dominant cost on these text-heavy PDFs (measured ~4x: 8.0s -> 2.0s on a
  26k-word doc) and extracts the *same words* -- only tabular grid formatting is
  lost, not the cell text -- so full-text/search quality is unchanged.
"""

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
        # Global module state in pymupdf4llm; set once per worker process.
        pymupdf4llm.use_layout(False)
        _layout_disabled = True

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        # Permission-restricted but readable PDFs open "encrypted"; an empty
        # owner password unlocks them so the classic path can read the text.
        if doc.needs_pass:
            doc.authenticate("")
        return pymupdf4llm.to_markdown(doc, table_strategy=None)


def _meta_title(data: bytes) -> str | None:
    """The PDF's own embedded title (``doc.metadata['title']``), raw. Consulted only
    when the markdown carried no ``# `` heading, so most PDFs never pay this open.
    Reused by the backfill (storage.run_backfill) to title existing PDFs."""
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

    # Strip the markdown syntax the same way the HTML path does. Using the raw
    # markdown as `text` made len(text.split()) count `#`, `|`, `---`, `-` and `>`
    # as words, so the shared min_words gate was reading two different notions of
    # "word" and a heading/table-heavy PDF passed it on punctuation alone.
    text = md.to_text(markdown)

    # Title chain: a leading `# ` heading, else the PDF's own (sanitised) metadata
    # title. The per-URL filename fallback lives in the write path / backfill --
    # the extractor only sees content-addressed bytes, never the URL.
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
