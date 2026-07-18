# Metadata Backfill (lang / final_url / PDF titles) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Populate three dead document-metadata fields — `lang`, `final_url`, and PDF/HTML `title` — on new extractions and, via a one-time `backfill` command, across the existing 46,389-doc corpus.

**Architecture:** Pure Phase-2 / Python. A new shared `lang.py` detector runs over the already-computed `text` in both extractors; the PDF extractor gains a `doc.metadata['title']` fallback; `_upsert_document` fills a filename-derived title when none is present. A new standalone `storage.run_backfill` / `dhbw-scraper backfill` command does one keyset-paginated pass over the existing corpus (never touching `updated_at`, idempotent), deriving `final_url` from `crawl_log` matched on `(url, sha256)`. **No Rust changes, no `maturin` rebuild, no schema change** — all four target columns already exist.

**Tech Stack:** Python 3.14, SQLite (via `sqlite3`), `py3langid` (new dep), `pymupdf` (already present), pytest, ruff.

**Design spec:** [`docs/superpowers/specs/2026-07-17-metadata-backfill-design.md`](../specs/2026-07-17-metadata-backfill-design.md)

## Global Constraints

- **Conventional Commits** enforced (commit-msg hook + CI). Use `feat`/`test`/`docs`/`build`/`chore`. Every commit subject must be a valid Conventional Commit.
- **End every commit message** with the trailer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **config.toml is the sole source of tuning values** — no new CLI flag may override a tuning value. `backfill` takes no tuning flags; its batch size comes from `config.dedup.batch_size` (as `reclassify` does).
- **No Rust / no schema change.** Do not touch `src/scrape-engine/`, `SCHEMA`, or `_migrate`. All four columns (`documents.lang`, `documents.final_url`, `documents.title`, `raw_docs.lang`) already exist.
- **Derived metadata never bumps `updated_at`** — the backfill must not spam `delta()`, exactly like `run_dedup` / `run_reclassify`.
- **Determinism**: language detection must be reproducible run-to-run (no random seed dependence).
- **Run tests with** `uv run pytest`; format/lint with `uv run ruff format` and `uv run ruff check`. Ruff line-length is 88.
- Python-source layout is `src/scraper/`; tests live in `tests/`.

---

## Task 1: `lang.py` language detector + `py3langid` dependency

**Files:**
- Create: `src/scraper/lang.py`
- Create: `tests/test_lang.py`
- Modify: `pyproject.toml` (add `py3langid` to `[project].dependencies`)

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces: `lang.detect(text: str | None) -> str | None` — best-effort lowercased ISO-639-1 code (`"de"`, `"en"`), or `None` when text is too short or detection is below the confidence floor. Deterministic. Used by Tasks 3, 4, 6.

- [ ] **Step 1: Add the dependency and install it**

Edit `pyproject.toml`, adding one line to `[project].dependencies` (keep the existing two):

```toml
dependencies = [
    "trafilatura>=2.1.0",
    # PyMuPDF4LLM converts PDF bytes -> Markdown. It is lightweight (no torch,
    # no ML models) and pulls in pymupdf transitively.
    "pymupdf4llm>=0.3.4",
    # Deterministic, offline language identification (ISO-639-1) over already-
    # extracted text. Pure-Python + numpy; used by scraper.lang for both extractors.
    "py3langid>=0.3",
]
```

Install it into the venv without forcing a Rust rebuild:

Run: `uv pip install "py3langid>=0.3"`
Expected: resolves and installs `py3langid` (and `numpy` if not already present). (On a full `uv sync` later, in the MSVC shell, this is reconciled; `uv pip install` here keeps the executing agent unblocked without recompiling `scraper._engine`.)

Verify the import works:

Run: `uv run python -c "from py3langid.langid import LanguageIdentifier, MODEL_FILE; i=LanguageIdentifier.from_pickled_model(MODEL_FILE, norm_probs=True); print(i.classify('This is a short English sentence for testing.'))"`
Expected: prints a tuple like `('en', 0.99...)`.

- [ ] **Step 2: Write the failing test**

Create `tests/test_lang.py`:

```python
from scraper import lang


def test_detects_german():
    text = (
        "Die Duale Hochschule Baden-Württemberg verbindet ein wissenschaftliches "
        "Studium mit der praktischen Ausbildung in einem Unternehmen."
    )
    assert lang.detect(text) == "de"


def test_detects_english():
    text = (
        "The cooperative state university combines academic study with practical "
        "training at a partner company over the course of three years."
    )
    assert lang.detect(text) == "en"


def test_empty_and_none_return_none():
    assert lang.detect("") is None
    assert lang.detect(None) is None
    assert lang.detect("   \n\t ") is None


def test_too_short_returns_none():
    # Below the character floor -> no trustworthy guess.
    assert lang.detect("Hallo") is None


def test_is_deterministic():
    text = "Studierende absolvieren Praxisphasen bei ihrem dualen Partner."
    assert lang.detect(text) == lang.detect(text)
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run pytest tests/test_lang.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scraper.lang'` (or `AttributeError` on `detect`).

- [ ] **Step 4: Write the implementation**

Create `src/scraper/lang.py`:

```python
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

        _identifier = LanguageIdentifier.from_pickled_model(
            MODEL_FILE, norm_probs=True
        )
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
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_lang.py -v`
Expected: PASS (5 passed).

- [ ] **Step 6: Format, lint, commit**

Run: `uv run ruff format src/scraper/lang.py tests/test_lang.py && uv run ruff check src/scraper/lang.py tests/test_lang.py`
Expected: no changes / no errors.

```bash
git add pyproject.toml src/scraper/lang.py tests/test_lang.py
git commit -m "feat(lang): add deterministic ISO-639-1 detector over extracted text" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `pdf_title.py` title helpers

**Files:**
- Create: `src/scraper/pdf_title.py`
- Create: `tests/test_pdf_title.py`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces:
  - `pdf_title.clean(raw: str | None) -> str | None` — trims a PDF metadata title, returning `None` for junk (empty, `"untitled"`/`"unbenannt"`, `"Microsoft Word - …"` artifacts, or a bare filename like `scan0001.pdf`).
  - `pdf_title.from_url(url: str) -> str | None` — a human-readable title from a URL's last path segment (url-decoded, extension stripped, `-`/`_` → spaces), or `None` when there is no usable basename.
  - Used by Tasks 4, 5, 6.

- [ ] **Step 1: Write the failing test**

Create `tests/test_pdf_title.py`:

```python
from scraper import pdf_title


def test_clean_trims_and_keeps_real_titles():
    assert pdf_title.clean("  Prüfungsordnung der DHBW  ") == "Prüfungsordnung der DHBW"


def test_clean_rejects_empty_and_none():
    assert pdf_title.clean(None) is None
    assert pdf_title.clean("") is None
    assert pdf_title.clean("   ") is None


def test_clean_rejects_placeholders():
    assert pdf_title.clean("untitled") is None
    assert pdf_title.clean("Unbenannt") is None


def test_clean_rejects_office_tool_artifacts():
    assert pdf_title.clean("Microsoft Word - Modulhandbuch.docx") is None


def test_clean_rejects_bare_filenames():
    assert pdf_title.clean("scan0001.pdf") is None
    assert pdf_title.clean("Modul.docx") is None


def test_from_url_derives_title_from_basename():
    url = "https://www.dhbw.de/fileadmin/Modulhandbuch_WI.pdf"
    assert pdf_title.from_url(url) == "Modulhandbuch WI"


def test_from_url_url_decodes_and_strips_extension():
    url = "https://www.dhbw.de/dateien/Amtliche%20Bekanntmachung.pdf"
    assert pdf_title.from_url(url) == "Amtliche Bekanntmachung"


def test_from_url_turns_hyphens_into_spaces():
    assert pdf_title.from_url("https://x/pruefungs-ordnung.pdf") == "pruefungs ordnung"


def test_from_url_returns_none_without_a_basename():
    assert pdf_title.from_url("https://www.dhbw.de/a/b/") is None
    assert pdf_title.from_url("https://www.dhbw.de/") is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_pdf_title.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scraper.pdf_title'`.

- [ ] **Step 3: Write the implementation**

Create `src/scraper/pdf_title.py`:

```python
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
    path = urlsplit(url).path.rstrip("/")
    base = unquote(path.rsplit("/", 1)[-1]) if path else ""
    base, _ext = os.path.splitext(base)
    base = _WS.sub(" ", base.replace("_", " ").replace("-", " ")).strip()
    return base or None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_pdf_title.py -v`
Expected: PASS (9 passed).

- [ ] **Step 5: Format, lint, commit**

Run: `uv run ruff format src/scraper/pdf_title.py tests/test_pdf_title.py && uv run ruff check src/scraper/pdf_title.py tests/test_pdf_title.py`
Expected: no changes / no errors.

```bash
git add src/scraper/pdf_title.py tests/test_pdf_title.py
git commit -m "feat(title): add PDF metadata-title cleaner and URL-basename title helper" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Detect language in `html_extract`

**Files:**
- Modify: `src/scraper/html_extract.py`
- Modify: `tests/test_html_extract.py`

**Interfaces:**
- Consumes: `lang.detect` (Task 1).
- Produces: `extract_html(...)` now returns a detected `"lang"` instead of hardcoded `None`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_html_extract.py`:

```python
def test_extract_html_detects_language():
    html = (
        "<html><body><main><p>"
        "Die Duale Hochschule Baden-Württemberg verbindet ein wissenschaftliches "
        "Studium mit der praktischen Berufsausbildung in einem Unternehmen und "
        "richtet sich an Studierende aus der ganzen Region."
        "</p></main></body></html>"
    )
    doc = extract_html(html)
    assert doc is not None
    assert doc["lang"] == "de"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_html_extract.py::test_extract_html_detects_language -v`
Expected: FAIL — `assert None == "de"` (current code hardcodes `"lang": None`).

- [ ] **Step 3: Implement**

In `src/scraper/html_extract.py`, add the import near the top (after the existing `from . import markdown as md`):

```python
from . import lang as langmod
```

Then in `extract_html`, replace the `"lang": None,` line with:

```python
        "lang": langmod.detect(text),
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_html_extract.py -v`
Expected: PASS (all, including the new test).

- [ ] **Step 5: Format, lint, commit**

Run: `uv run ruff format src/scraper/html_extract.py tests/test_html_extract.py && uv run ruff check src/scraper/html_extract.py tests/test_html_extract.py`
Expected: no changes / no errors.

```bash
git add src/scraper/html_extract.py tests/test_html_extract.py
git commit -m "feat(extract): detect language for HTML documents" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Detect language + metadata-title fallback in `pdf_extract`

**Files:**
- Modify: `src/scraper/pdf_extract.py`
- Modify: `tests/test_pdf_extract.py`

**Interfaces:**
- Consumes: `lang.detect` (Task 1), `pdf_title.clean` (Task 2).
- Produces:
  - `pdf_extract._meta_title(data: bytes) -> str | None` — opens the PDF and returns its raw `doc.metadata['title']` (used as the default metadata seam here and reused by Task 6).
  - `extract_pdf(data, to_markdown=None, meta_title=None)` — now returns a detected `"lang"`, and its title chain is: leading `# ` heading → `pdf_title.clean(meta_title(data))` → `None`. `meta_title` is only invoked when the heading is absent.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_pdf_extract.py`:

```python
def test_extract_pdf_detects_language():
    text_md = (
        "# Modulhandbuch\n\n"
        "Die Studierenden erwerben in diesem Modul grundlegende Kenntnisse der "
        "Wirtschaftsinformatik und wenden sie in praktischen Projekten an."
    )
    doc = extract_pdf(b"%PDF fake", to_markdown=lambda data: text_md)
    assert doc["lang"] == "de"


def test_extract_pdf_falls_back_to_metadata_title_when_no_heading():
    # Markdown without any "# " heading -> the metadata title is consulted.
    no_heading = "Inhalt ohne Ueberschrift, aber mit ausreichend echtem Fliesstext."
    doc = extract_pdf(
        b"%PDF fake",
        to_markdown=lambda data: no_heading,
        meta_title=lambda data: "  Studien- und Pruefungsordnung  ",
    )
    assert doc["title"] == "Studien- und Pruefungsordnung"


def test_extract_pdf_rejects_junk_metadata_title():
    doc = extract_pdf(
        b"%PDF fake",
        to_markdown=lambda data: "Fliesstext ohne jede Ueberschrift in dem Dokument.",
        meta_title=lambda data: "Microsoft Word - egal.docx",
    )
    assert doc["title"] is None  # junk metadata rejected; filename fallback is per-URL


def test_extract_pdf_prefers_heading_over_metadata():
    called = {"meta": 0}

    def meta(data):
        called["meta"] += 1
        return "Metadata Title"

    doc = extract_pdf(
        b"%PDF fake",
        to_markdown=lambda data: "# Heading Wins\n\nEnough real body text here too.",
        meta_title=meta,
    )
    assert doc["title"] == "Heading Wins"
    assert called["meta"] == 0  # metadata is not read when a heading exists
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_pdf_extract.py -v`
Expected: FAIL — `test_extract_pdf_detects_language` fails (`lang` is `None`); the metadata tests fail on unexpected `meta_title` keyword / wrong title.

- [ ] **Step 3: Implement**

Rewrite `src/scraper/pdf_extract.py` from the imports down. Add the two new imports and the `_meta_title` reader, split the heading logic into `_heading_title`, and thread the new seam. Full replacement of the module body below the existing module docstring:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_pdf_extract.py -v`
Expected: PASS (all, including the four new tests; the existing heading/word-count tests still pass because the heading path is unchanged and `meta_title` is not called when a heading exists).

- [ ] **Step 5: Format, lint, commit**

Run: `uv run ruff format src/scraper/pdf_extract.py tests/test_pdf_extract.py && uv run ruff check src/scraper/pdf_extract.py tests/test_pdf_extract.py`
Expected: no changes / no errors.

```bash
git add src/scraper/pdf_extract.py tests/test_pdf_extract.py
git commit -m "feat(extract): detect language and add PDF metadata-title fallback" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Filename title fallback in `_upsert_document`

**Files:**
- Modify: `src/scraper/storage.py:844-943` (`_upsert_document`)
- Modify: `tests/test_storage_docs.py`

**Interfaces:**
- Consumes: `pdf_title.from_url` (Task 2). `storage.py` already imports `from . import classify, taxonomy`; add `pdf_title` there.
- Produces: on a `"new"` or `"changed"` document, a still-missing title is filled from the URL basename. The `"unchanged"`/`"duplicate"` branches are deliberately untouched (that is the backfill's job in Task 6).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_storage_docs.py` (the module already imports `from scraper import storage as st` and defines `mem()` and `NOW1`):

```python
def test_upsert_fills_missing_title_from_url_basename():
    conn = mem()
    st.enqueue(conn, "https://x/fileadmin/Modulhandbuch_WI.pdf", "x", 0, None, NOW1)
    titleless = {
        "title": None,
        "text": "genuine module handbook body text " * 10,
        "markdown": "genuine module handbook body text " * 10,
        "lang": None,
        "word_count": 60,
        "metadata": None,
    }
    st.upsert_document(
        conn, "https://x/fileadmin/Modulhandbuch_WI.pdf", "x", "pdf", "c1", titleless, NOW1
    )
    row = conn.execute(
        "SELECT title FROM documents WHERE url='https://x/fileadmin/Modulhandbuch_WI.pdf'"
    ).fetchone()
    assert row["title"] == "Modulhandbuch WI"


def test_upsert_keeps_existing_title():
    conn = mem()
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW1)
    withtitle = {
        "title": "Real Title",
        "text": "some body text here " * 10,
        "markdown": "some body text here " * 10,
        "lang": None,
        "word_count": 40,
        "metadata": None,
    }
    st.upsert_document(conn, "https://x/a", "x", "html", "c1", withtitle, NOW1)
    row = conn.execute("SELECT title FROM documents WHERE url='https://x/a'").fetchone()
    assert row["title"] == "Real Title"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_storage_docs.py::test_upsert_fills_missing_title_from_url_basename -v`
Expected: FAIL — stored title is `None` (current insert binds `doc.get("title")` verbatim).

- [ ] **Step 3: Implement**

In `src/scraper/storage.py`, extend the top-of-file import:

```python
from . import classify, pdf_title, taxonomy
```

In `_upsert_document`, immediately after the line `meta = json.dumps(doc.get("metadata")) if doc.get("metadata") else None`, add:

```python
    # A doc the extractor could not title (a PDF with no heading and no usable
    # metadata title, or a rare title-less HTML page) still gets a readable title
    # from its URL basename. The "unchanged"/"duplicate" branches below never write
    # title -- the one-time backfill (run_backfill) covers already-stored rows.
    title = doc.get("title") or pdf_title.from_url(url)
```

Then in the `existing is None` INSERT, replace `doc.get("title"),` with `title,`. And in the `existing["text_sha256"] != h` (changed) UPDATE, replace its `doc.get("title"),` with `title,`. (Both occurrences — leave every other column exactly as-is.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_storage_docs.py -v`
Expected: PASS (all — the two new tests plus the existing lifecycle tests, whose `doc()` fixture always sets `title="T"` so they are unaffected).

- [ ] **Step 5: Format, lint, commit**

Run: `uv run ruff format src/scraper/storage.py tests/test_storage_docs.py && uv run ruff check src/scraper/storage.py tests/test_storage_docs.py`
Expected: no changes / no errors.

```bash
git add src/scraper/storage.py tests/test_storage_docs.py
git commit -m "feat(storage): fill missing document titles from the URL basename on upsert" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: `storage.run_backfill` — the one-time corpus repair

**Files:**
- Modify: `src/scraper/storage.py` (add `_backfill_title` + `run_backfill` near `run_reclassify`)
- Create: `tests/test_backfill.py`

**Interfaces:**
- Consumes: `lang.detect` (Task 1), `pdf_title.clean`/`from_url` (Task 2), `pdf_extract._meta_title` (Task 4), `fetch.ext_for`, `RawCache`, `write_txn`.
- Produces: `storage.run_backfill(conn, raw_dir, batch_size: int = 500) -> dict` returning `{"lang": int, "final_url": int, "titles": int, "scanned": int}`. Used by Task 7.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_backfill.py`:

```python
"""Tests for the one-time metadata backfill (storage.run_backfill).

Seeds `documents` rows with the three fields dead exactly as the real corpus has
them (lang NULL, final_url == url, title NULL) plus the crawl_log rows the redirect
truth is recovered from, then checks each field is repaired without bumping
updated_at, and that a second run is a no-op.
"""

from scraper import storage as st

NOW1 = "2026-07-14T00:00:00"
RUN = "run-1"


def db(tmp_path):
    conn = st.connect(str(tmp_path / "backfill.sqlite3"))
    st.init_db(conn)
    return conn


def insert_doc(conn, url, text, sha, source_type="html", title=None, present=1, now=NOW1):
    """A document row with lang NULL, final_url == url, title as given (default NULL)."""
    conn.execute(
        """INSERT INTO documents (id, url, final_url, site, source_type,
               content_sha256, title, text, markdown, lang, word_count, metadata,
               text_sha256, present, revision, first_indexed_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,NULL,?,NULL,?,?,1,?,?)""",
        (
            st._doc_id(url), url, url, "x", source_type, sha, title, text, text,
            len(text.split()), st.text_hash(text), present, now, now,
        ),
    )
    conn.commit()


def log_fetch(conn, url, final_url, sha, now=NOW1, status=200):
    st.record_fetch(
        conn, RUN, url, final_url, "x", status, "text/html", sha, 100, "html",
        "changed", None, now,
    )


def test_backfill_populates_lang(tmp_path):
    conn = db(tmp_path)
    insert_doc(
        conn, "https://x/a",
        "Die Studierenden absolvieren Praxisphasen bei ihrem dualen Partner im "
        "Unternehmen und lernen die Praxis kennen.", "c1",
    )
    result = st.run_backfill(conn, tmp_path / "raw")
    assert result["lang"] == 1
    row = conn.execute("SELECT lang FROM documents WHERE url='https://x/a'").fetchone()
    assert row["lang"] == "de"


def test_backfill_sets_final_url_from_matching_crawl_log(tmp_path):
    conn = db(tmp_path)
    insert_doc(conn, "https://x/login-page", "some body text here " * 10, "c1")
    log_fetch(conn, "https://x/login-page", "https://x/auth/signin", "c1")

    result = st.run_backfill(conn, tmp_path / "raw")

    assert result["final_url"] == 1
    row = conn.execute(
        "SELECT final_url FROM documents WHERE url='https://x/login-page'"
    ).fetchone()
    assert row["final_url"] == "https://x/auth/signin"


def test_backfill_final_url_ignores_later_304_and_matches_bytes(tmp_path):
    """A later 304 recheck logs final_url == url; the sha match must keep the real
    redirect from the full fetch that produced this doc's bytes."""
    conn = db(tmp_path)
    insert_doc(conn, "https://x/p", "body text of the page " * 10, "c1")
    log_fetch(conn, "https://x/p", "https://x/real-target", "c1", now="2026-07-14T00:00:00")
    # a later 304 row: build_batch writes final_url == url and sha == the cached sha
    st.record_fetch(
        conn, "run-2", "https://x/p", "https://x/p", "x", 304, None, "c1", 0,
        None, "unchanged", None, "2026-07-15T00:00:00",
    )
    st.run_backfill(conn, tmp_path / "raw")
    row = conn.execute("SELECT final_url FROM documents WHERE url='https://x/p'").fetchone()
    assert row["final_url"] == "https://x/real-target"


def test_backfill_leaves_final_url_when_no_redirect(tmp_path):
    conn = db(tmp_path)
    insert_doc(conn, "https://x/plain", "ordinary page body text " * 10, "c1")
    log_fetch(conn, "https://x/plain", "https://x/plain", "c1")  # no redirect
    result = st.run_backfill(conn, tmp_path / "raw")
    assert result["final_url"] == 0
    row = conn.execute("SELECT final_url FROM documents WHERE url='https://x/plain'").fetchone()
    assert row["final_url"] == "https://x/plain"


def test_backfill_titles_missing_html_from_url(tmp_path):
    conn = db(tmp_path)
    insert_doc(conn, "https://x/pruefungs-ordnung", "body " * 20, "c1", source_type="html")
    result = st.run_backfill(conn, tmp_path / "raw")
    assert result["titles"] == 1
    row = conn.execute(
        "SELECT title FROM documents WHERE url='https://x/pruefungs-ordnung'"
    ).fetchone()
    assert row["title"] == "pruefungs ordnung"


def test_backfill_pdf_title_prefers_cached_metadata(tmp_path, monkeypatch):
    conn = db(tmp_path)
    insert_doc(conn, "https://x/doc.pdf", "pdf body text here " * 10, "cpdf", source_type="pdf")
    # a cached blob must exist at the content-addressed path for metadata to be read
    cache = st.RawCache(tmp_path / "raw")
    path = cache.path_for("cpdf", ".pdf")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF fake bytes")
    from scraper import pdf_extract
    monkeypatch.setattr(pdf_extract, "_meta_title", lambda data: "Embedded Ordnung Title")

    st.run_backfill(conn, tmp_path / "raw")

    row = conn.execute("SELECT title FROM documents WHERE url='https://x/doc.pdf'").fetchone()
    assert row["title"] == "Embedded Ordnung Title"


def test_backfill_pdf_title_falls_back_to_url_when_blob_missing(tmp_path):
    conn = db(tmp_path)
    # no cached blob on disk -> from_url fallback
    insert_doc(conn, "https://x/fileadmin/Modul_Handbuch.pdf", "pdf body " * 20, "cx", source_type="pdf")
    st.run_backfill(conn, tmp_path / "raw")
    row = conn.execute(
        "SELECT title FROM documents WHERE url='https://x/fileadmin/Modul_Handbuch.pdf'"
    ).fetchone()
    assert row["title"] == "Modul Handbuch"


def test_backfill_does_not_bump_updated_at(tmp_path):
    conn = db(tmp_path)
    insert_doc(conn, "https://x/a", "German body ist hier drin genug Text vorhanden " * 5, "c1")
    log_fetch(conn, "https://x/a", "https://x/redirected", "c1")
    st.run_backfill(conn, tmp_path / "raw")
    row = conn.execute("SELECT updated_at FROM documents WHERE url='https://x/a'").fetchone()
    assert row["updated_at"] == NOW1


def test_backfill_is_idempotent(tmp_path):
    conn = db(tmp_path)
    insert_doc(conn, "https://x/a", "genug deutscher Fliesstext fuer die Erkennung hier " * 4, "c1")
    log_fetch(conn, "https://x/a", "https://x/z", "c1")
    first = st.run_backfill(conn, tmp_path / "raw")
    assert first["lang"] == 1 and first["final_url"] == 1 and first["titles"] == 1
    second = st.run_backfill(conn, tmp_path / "raw")
    assert second["lang"] == 0 and second["final_url"] == 0 and second["titles"] == 0


def test_backfill_skips_removed_rows(tmp_path):
    conn = db(tmp_path)
    insert_doc(conn, "https://x/gone", "removed body text here " * 10, "c1", present=0)
    result = st.run_backfill(conn, tmp_path / "raw")
    assert result["scanned"] == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_backfill.py -v`
Expected: FAIL — `AttributeError: module 'scraper.storage' has no attribute 'run_backfill'`.

- [ ] **Step 3: Implement**

In `src/scraper/storage.py`, add these two functions after `run_reclassify` (end of file):

```python
def _backfill_title(row, cache, ext_for) -> str | None:
    """Best title for a title-less document: for a PDF, the cached blob's cleaned
    embedded metadata title, else -- and for HTML -- the URL basename."""
    if row["source_type"] == "pdf":
        path = cache.path_for(row["content_sha256"], ext_for("pdf"))
        try:
            data = path.read_bytes()
        except OSError:
            data = None
        if data is not None:
            from . import pdf_extract

            cleaned = pdf_title.clean(pdf_extract._meta_title(data))
            if cleaned:
                return cleaned
    return pdf_title.from_url(row["url"])


def run_backfill(conn, raw_dir, batch_size: int = 500) -> dict:
    """One-time repair of the three dead metadata fields over the *existing* corpus,
    in one keyset-paginated pass over present ``documents``. Idempotent and it
    **never touches ``updated_at``** (derived metadata must not spam :func:`delta`),
    exactly like :func:`run_dedup` / :func:`run_reclassify`. Re-crawl/re-extract
    cannot do this: none of these fields changes ``text_sha256``, so an unchanged
    doc takes the no-op branch of :func:`_upsert_document`.

    * ``lang``: detect from the stored ``text`` where NULL (skip if undetectable).
    * ``final_url``: the true redirect target from ``crawl_log``, taken from the
      genuine full fetch (``status=200``) of the exact bytes this doc holds
      (``url`` + ``sha256``); the ``status=200`` filter keeps a later 304 recheck
      (which logs ``final_url == url``) from masking the real redirect. Docs with no
      matching full-fetch row keep ``final_url == url`` -- never NULL, never wrong.
    * ``title``: for a title-less doc, a PDF's cleaned embedded metadata title (from
      the re-opened cached blob) else the URL basename; HTML uses the URL basename.

    Do NOT run concurrently with fetch/extract -- all three write ``documents``.
    """
    from . import fetch as fetchmod

    cache = RawCache(raw_dir)

    # Step A: (url, sha256) -> final_url, built by streaming crawl_log once (never
    # resident) and keeping only keys that belong to a present doc. Later rows (a
    # larger id, i.e. a more recent fetch of those exact bytes) win.
    doc_keys = {
        (r["url"], r["content_sha256"])
        for r in conn.execute(
            "SELECT url, content_sha256 FROM documents WHERE present=1"
        )
    }
    final_by_key: dict = {}
    for r in conn.execute(
        # status=200 selects genuine full-fetch rows only. A 304 recheck also logs
        # sha256 == the cached content digest but with final_url == the request URL
        # (see scrape-engine/crawl.rs build_batch, 304 branch), so without this filter
        # a later 304 would overwrite the real redirect this doc's bytes resolved to.
        "SELECT url, sha256, final_url FROM crawl_log "
        "WHERE sha256 IS NOT NULL AND final_url IS NOT NULL AND status = 200 "
        "ORDER BY id"
    ):
        key = (r["url"], r["sha256"])
        if key in doc_keys:
            final_by_key[key] = r["final_url"]

    # Step B: keyset pass over present docs (single O(n) forward scan by PK id).
    counts = {"lang": 0, "final_url": 0, "titles": 0, "scanned": 0}
    last_id = ""
    while True:
        rows = conn.execute(
            "SELECT id, url, source_type, content_sha256, text, title, lang, final_url "
            "FROM documents WHERE present=1 AND id > ? ORDER BY id LIMIT ?",
            (last_id, batch_size),
        ).fetchall()
        if not rows:
            break
        pending = []  # (sets, id) computed outside the write lock
        for r in rows:
            counts["scanned"] += 1
            sets: dict = {}
            if r["lang"] is None:
                detected = lang.detect(r["text"])
                if detected is not None:
                    sets["lang"] = detected
            fu = final_by_key.get((r["url"], r["content_sha256"]))
            if fu is not None and fu != r["final_url"]:
                sets["final_url"] = fu
            if not r["title"]:
                new_title = _backfill_title(r, cache, fetchmod.ext_for)
                if new_title:
                    sets["title"] = new_title
            if sets:
                counts["lang"] += "lang" in sets
                counts["final_url"] += "final_url" in sets
                counts["titles"] += "title" in sets
                pending.append((sets, r["id"]))
        if pending:
            with write_txn(conn):
                for sets, doc_id in pending:
                    assignments = ", ".join(f"{col}=?" for col in sets)
                    conn.execute(
                        f"UPDATE documents SET {assignments} WHERE id=?",
                        (*sets.values(), doc_id),
                    )
        last_id = rows[-1]["id"]
    return counts
```

Add the `lang` import at the top of `storage.py` alongside the existing imports:

```python
from . import classify, lang, pdf_title, taxonomy
```

(`counts["lang"] += "lang" in sets` adds the boolean `True`/`False` as `1`/`0` — a standard Python idiom; ruff will not object.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_backfill.py -v`
Expected: PASS (10 passed).

- [ ] **Step 5: Format, lint, commit**

Run: `uv run ruff format src/scraper/storage.py tests/test_backfill.py && uv run ruff check src/scraper/storage.py tests/test_backfill.py`
Expected: no changes / no errors.

```bash
git add src/scraper/storage.py tests/test_backfill.py
git commit -m "feat(storage): backfill lang, final_url, and titles over the existing corpus" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: `backfill` CLI command + docs

**Files:**
- Modify: `src/scraper/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `README.md`, `CLAUDE.md`

**Interfaces:**
- Consumes: `storage.run_backfill` (Task 6). Reads `config.storage.raw_dir` and `config.dedup.batch_size`.
- Produces: `dhbw-scraper backfill` subcommand (no tuning flags; global `--config` only).

- [ ] **Step 1: Write the failing tests**

In `tests/test_cli.py`, add `["backfill"]` to the tuple in `test_parser_has_all_subcommands` (so it reads, after `["dedup"],`):

```python
        ["dedup"],
        ["backfill"],
        ["delta", "--since", "2026-01-01"],
```

Then add a dispatch test (mirrors `test_dedup_command_uses_config_values`):

```python
def test_backfill_command_dispatches_with_config_values(tmp_path, monkeypatch):
    _write_config(tmp_path, dedup_extra="batch_size = 9")
    captured = {}

    def fake_run_backfill(conn, raw_dir, batch_size=500):
        captured["raw_dir"] = str(raw_dir)
        captured["batch_size"] = batch_size
        return {"lang": 0, "final_url": 0, "titles": 0, "scanned": 0}

    monkeypatch.setattr(cli.storage, "run_backfill", fake_run_backfill)
    rc = cli.main(["--config", str(tmp_path / "config.toml"), "backfill"])

    assert rc == 0
    assert captured["batch_size"] == 9
    assert captured["raw_dir"].endswith("raw")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli.py::test_parser_has_all_subcommands tests/test_cli.py::test_backfill_command_dispatches_with_config_values -v`
Expected: FAIL — `backfill` is not a known subcommand (argparse `SystemExit`).

- [ ] **Step 3: Implement**

In `src/scraper/cli.py`, add the command handler after `_cmd_reclassify`:

```python
def _cmd_backfill(args) -> int:
    config = _load(args)
    conn = storage.connect(config.storage.db_file)
    storage.init_db(conn)
    result = storage.run_backfill(
        conn, config.storage.raw_dir, batch_size=config.dedup.batch_size
    )
    print(json.dumps(result, indent=2))
    conn.close()
    return 0
```

And register the subparser in `build_parser`, after the `reclassify` (`rc`) block and before `delta`:

```python
    bf = sub.add_parser(
        "backfill",
        help="One-time repair of dead metadata (lang / final_url / titles) across "
        "the existing corpus. Idempotent; never touches updated_at. Do not run "
        "while fetch/extract is running.",
    )
    bf.set_defaults(func=_cmd_backfill)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS (all).

- [ ] **Step 5: Update the docs**

In `CLAUDE.md`, in the "CLI (`uv run dhbw-scraper <cmd>`)" paragraph, add `backfill` to the command list next to `reclassify`, e.g. after the `reclassify (...)` clause insert:

```
· `backfill` (one-time: populate the dead `lang`/`final_url`/`title` metadata across
the existing corpus from stored text + `crawl_log` + the raw cache; idempotent, never
touches `updated_at`)
```

In `README.md`, find the CLI/Usage command list (where `dedup` and `reclassify` are documented) and add an entry in the same style:

```
- `backfill` — one-time maintenance pass that populates the three previously-unwritten
  metadata fields on existing `documents`: `lang` (detected from stored text),
  `final_url` (the real redirect target, recovered from `crawl_log`), and a `title` for
  title-less docs (PDF embedded metadata → URL basename). Idempotent, keyset-paginated,
  and never bumps `updated_at`. Do not run while `fetch`/`extract` is writing.
```

(Match the surrounding bullet/formatting style of the file you are editing; the exact wording above is the content, not the required markup.)

- [ ] **Step 6: Run the full suite + lint**

Run: `uv run pytest`
Expected: PASS (entire suite green — Phase-2 + CLI + native e2e).

Run: `uv run ruff format --check . && uv run ruff check .`
Expected: no changes / no errors.

- [ ] **Step 7: Commit**

```bash
git add src/scraper/cli.py tests/test_cli.py README.md CLAUDE.md
git commit -m "feat(cli): add backfill command for dead metadata fields" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Operator runbook (live pass — run by the operator, not the agent)

Per project preference, the live pass over the real 4 GB DB + raw cache is operator-run. In the MSVC-enabled shell:

```powershell
uv sync --extra dev            # reconciles py3langid into the locked env (rebuilds the extension harmlessly)
uv run pytest                  # green before touching the corpus
# (optional) copy data/scraper.sqlite3 aside as a safety net first
uv run dhbw-scraper backfill   # populates lang + final_url + titles; prints JSON counts
uv run dhbw-scraper stats
uv run dhbw-scraper report -o data/analysis.html
```

Expected shape: `lang` populated on ~46k docs (a NULL tail for thin/undetectable text), `final_url` corrected on ~8k redirected docs, `titles` filled on ~1,097. Safe to re-run.

---

## Self-Review (completed by plan author)

**Spec coverage:** D1 lang → Tasks 1, 3, 4 (forward) + 6 (backfill). D2 final_url → Task 6 (Python-only, from crawl_log, sha-matched). D3 titles → Tasks 2, 4 (metadata), 5 (forward filename), 6 (backfill metadata→filename). One `backfill` command → Tasks 6–7. `py3langid` dep → Task 1. Standalone (not chained into `run`) → Task 7 (no `_cmd_run` change). No Rust / no schema / no `updated_at` bump → enforced across Tasks 5–6. Docs → Task 7. All spec sections map to a task.

**Placeholder scan:** none — every code and test step contains complete, runnable content.

**Type consistency:** `lang.detect(text)->str|None`, `pdf_title.clean(raw)->str|None`, `pdf_title.from_url(url)->str|None`, `pdf_extract._meta_title(data)->str|None`, `pdf_extract.extract_pdf(data, to_markdown=None, meta_title=None)`, `storage.run_backfill(conn, raw_dir, batch_size=500)->dict` with keys `{"lang","final_url","titles","scanned"}` — used identically everywhere they appear (Tasks 6 and 7 agree on the signature and the return keys).
