# Design — Fix three dead metadata fields (`lang`, `final_url`, PDF titles)

*Spec date: 2026-07-17 · Branch: `feat/create-dh-scraper` · Corresponds to corpus-audit
findings D1, D2, D3 in [`docs/corpus-audit-2026-07-17.md`](../../corpus-audit-2026-07-17.md).*

## Problem

Three document-metadata fields are dead in the materialized corpus (46,389 present docs):

- **D1 — `lang` is NULL for 100% of docs.** Never implemented:
  [`html_extract.py`](../../../src/scraper/html_extract.py) and
  [`pdf_extract.py`](../../../src/scraper/pdf_extract.py) both hardcode `"lang": None`;
  storage persists it faithfully. The corpus is genuinely mixed-language (≥456 docs under
  `/en/`), so language-filtered retrieval is impossible until this is populated. No language
  detector is currently installed (`py3langid`/`langdetect`/`fasttext` all absent; trafilatura
  2.1 ships without one).
- **D2 — `final_url` is a verbatim copy of `url` for every doc.**
  [`_upsert_document`](../../../src/scraper/storage.py) binds `url` into *both* columns on
  insert and never corrects it, while `crawl_log` recorded 8,321 real redirects. Downstream
  cannot tell that ~23 pages redirected to a login form and 2 to a 404; they are indexed under
  their original URL with junk titles.
- **D3 — 1,097 docs (1,085 PDF + 12 HTML) have no title.** The PDF extractor derives a title
  only from a leading `# ` markdown heading and never consults `doc.metadata['title']` or the
  filename. Titles feed retrieval ranking, so blanks hurt recall.

### The load-bearing constraint

Re-crawl / re-extract will **not** repair the existing corpus. Change detection keys on
`text_sha256`; none of these three fields alters the extracted text, so an unchanged doc takes
the "unchanged" branch of `_upsert_document`, which never rewrites `lang`, `title`, or
`final_url`. **A one-time backfill pass over the existing `documents` rows is therefore
mandatory**, independent of any future crawl. This mirrors how `dedup` (backfills `text_sha256`)
and `reclassify` (re-tags classification columns) already work: keyset-paginated maintenance
passes that never touch `updated_at` (derived metadata must not spam `delta()`).

## Goals / non-goals

**Goals**
- Populate `lang` (ISO-639-1) on all present docs and on newly extracted docs going forward.
- Populate `final_url` with the true redirect target on all present docs and going forward.
- Give every present title-less doc a title (PDF metadata → filename fallback).
- One new re-runnable, idempotent `backfill` maintenance command that does all three over the
  existing corpus.
- Pure Phase-2 / Python. **No Rust changes, no `maturin` rebuild.**

**Non-goals (explicitly out of scope)**
- No re-crawl, no re-extract (backfill reads what is already stored + the raw cache).
- No changes to Phase-1 Rust (`scrape-engine`), the queue schema, or the fetch path.
- Not backfilling `raw_docs.lang` (a Phase-2 cache; downstream reads `documents`). Forward
  extraction populates it as a side effect; existing rows stay NULL — acceptable.
- The other audit items (classification defects B1–B5, coverage re-crawls, corruption C1–C5,
  integrity E1–E4). Separate tracks.
- The 4 Office files mistyped as `source_type='html'` (D3 note). Left as-is.

## Design decisions (approved)

| Decision | Choice |
|---|---|
| `final_url` source | **Python-only, from `crawl_log`.** No Rust, no rebuild. |
| Backfill packaging | **One `backfill` command** covering all three fields in one pass. |
| PDF title effort | **Metadata then filename** — re-open cached PDF bytes for `doc.metadata['title']`, else a cleaned filename from the URL. |
| Language detector | **`py3langid`** (new direct dependency): deterministic by default, ISO-639-1, lightweight. One detector over the shared extracted `text` for both HTML and PDF. |
| Command chaining | `backfill` is **standalone** (like `reclassify`), **not** auto-chained into `run`. Rationale below. |

## Components

### 1. `src/scraper/lang.py` (new) — shared language detector

```python
def detect(text: str) -> str | None:
    """ISO-639-1 code (e.g. "de", "en") for `text`, or None when the text is too
    short/empty or detection is not confident enough to be useful."""
```

- Lazy-imports `py3langid` (mirrors the lazy `trafilatura`/`pymupdf` imports in the extractors)
  and holds one module-global normalized identifier (`norm_probs=True`) so confidence is a
  probability in `[0, 1]`.
- Returns `None` when: text is empty/whitespace after strip, is below a small minimum length
  floor, or the top language's probability is below a confidence floor. Storing `None` beats
  storing a wrong guess — downstream can treat NULL as "unknown".
- Returns a lowercased 2-letter code otherwise. Detection runs over the **already-computed
  plain `text`** (post-`markdown.to_text`), so HTML and PDF share one notion of language, just
  as they already share one notion of "word".
- Add `py3langid>=0.3` to `[project].dependencies` in `pyproject.toml`.

### 2. Extractor changes (forward path)

**`html_extract.extract_html`** — replace `"lang": None` with `lang.detect(text)`.

**`pdf_extract.extract_pdf`** — two changes:
- `"lang": None` → `lang.detect(text)`.
- Title fallback chain: (a) leading `# ` markdown heading (current behavior) → (b) cleaned
  `doc.metadata['title']` → (c) `None`. The filename fallback is per-URL and lives in the write
  path (§4), not here — the extractor only sees content-addressed bytes, never the URL.
- The PDF metadata read is added as a second injectable seam so tests stay offline:
  `extract_pdf(data, to_markdown=None, meta_title=None)`. The real `meta_title` reader opens the
  PDF and returns a cleaned `doc.metadata['title']`. It is only invoked when the markdown heading
  is absent, so the common case pays no extra PDF open.

### 3. `src/scraper/pdf_title.py` (new) — title helpers (shared by extractor + backfill)

```python
def clean(raw: str | None) -> str | None:
    """Trim/normalize a PDF metadata title; reject junk (empty, whitespace-only,
    tool artifacts like 'Microsoft Word - …', 'untitled', a bare filename)."""

def from_url(url: str) -> str | None:
    """Human-readable title from a URL's last path segment: url-decode, strip the
    extension, turn '-'/'_'/'%20' into spaces, collapse whitespace. None if there
    is no usable basename."""
```

### 4. `_upsert_document` — forward filename fallback

When a doc is inserted (`"new"`) or its text changes (`"changed"`) and `doc.get("title")` is
falsy, set the stored title to `pdf_title.from_url(url)`. This completes titles for
newly-materialized docs (metadata was already tried in the extractor; only the per-URL filename
remains). The "unchanged" branch is deliberately left alone — that is the branch the mandatory
backfill (§5) exists to cover.

### 5. `storage.run_backfill` + `backfill` CLI command (the one-time corpus fix)

A single keyset-paginated pass over present `documents`, modeled on `run_reclassify` /
`run_dedup`. Never touches `updated_at`. Idempotent (a second run is a no-op / same result).

```python
def run_backfill(conn, batch_size: int = 500) -> dict:
    # returns {"lang": n, "final_url": n, "titles": n, "scanned": n}
```

**Step A — build the `final_url` lookup (index-free, single scan).**
Load the `(url, content_sha256)` keys of all present docs into a dict. Stream `crawl_log`
ordered by `id` — `SELECT url, sha256, final_url FROM crawl_log WHERE sha256 IS NOT NULL` — and
for every row whose `(url, sha256)` is a live doc key, record its `final_url` (later rows win, so
the dict ends holding the max-`id` = most recent full fetch for those exact bytes). This matches
`final_url` to the precise fetch that produced the doc's current content, so a later 304 recheck
(whose `crawl_log.final_url` is just the request URL) cannot mask the real redirect. No new
index; memory is bounded by the ~46k live keys, not the ~1M-row `crawl_log`.

**Step B — per-batch update.** Keyset-paginate `documents` by `id`. For each present row:
- **lang**: if `lang IS NULL`, compute `lang.detect(text)` and set it (skip if detect returns
  None).
- **final_url**: if the Step-A dict has a value for `(url, content_sha256)` and it differs from
  the stored `final_url`, set it. (Docs with no matching `crawl_log` row keep `final_url = url` —
  never NULL, never wrong.)
- **title**: if `title` is NULL/empty, for a PDF re-open its cached blob
  (`RawCache.path_for(content_sha256, ext_for('pdf'))`) → `pdf_title.clean(doc.metadata['title'])`,
  else `pdf_title.from_url(url)`; for HTML use `pdf_title.from_url(url)`. If the cached blob is
  missing/unreadable, fall back to `from_url`. This clears all 1,097.
- Write only the columns that actually changed, all in one `write_txn` per batch.

**CLI**: `dhbw-scraper backfill` in [`cli.py`](../../../src/scraper/cli.py), alongside
`dedup`/`reclassify`: `_load` config → `connect` → `init_db` → `run_backfill` → print JSON counts.

### 6. Why standalone, not chained into `run`

`run` is `fetch → extract → dedup`. `dedup` is already an O(corpus) pass, so precedent exists,
but `backfill` re-reading `text` for detection and re-opening PDFs on every incremental run is
disproportionate. `reclassify` set the pattern: derived-metadata maintenance is a deliberate,
operator-invoked pass. `backfill` follows it. (Adding it to `_cmd_run` later is a one-liner if
desired — flagged as an open choice for spec review.)

## Data flow

```
Forward (new/changed docs):
  fetch (Rust) ──▶ crawl_log.final_url, raw_docs
  extract (Py)  ──▶ html/pdf_extract: text, lang=detect(text), title=heading|meta
  _upsert_document ──▶ documents.{lang, title(+from_url if still empty)}   [final_url still = url]

Backfill (existing corpus, one-time + re-runnable):
  documents (present) ─┬─ lang    ← detect(stored text)
                       ├─ final_url ← crawl_log where (url, sha256) matches this doc's bytes
                       └─ title   ← reopen cached PDF meta → from_url(url)
```

## Error handling / edge cases

- **Detector failure / thin text** → `lang` stays NULL (never a wrong guess).
- **No `crawl_log` match for a doc** (pruned log, E1 anomaly) → `final_url` left = `url`.
- **304 rows** carry `final_url == url`; the `sha256` match in Step A skips them in favor of the
  full-fetch row that produced the bytes.
- **Missing/unreadable cached PDF blob** during title backfill → filename fallback, no crash.
- **Idempotency**: re-running `backfill` recomputes the same values; guarded by `IS NULL` /
  "differs from stored" so a settled corpus performs no writes.
- **`present=0` tombstones** are skipped (backfill scopes to `present=1`).
- **Concurrency**: like `dedup`/`reclassify`, must not run while fetch/extract writes
  `documents`. Documented in the command help.

## Testing (TDD — tests before implementation)

- `lang.detect`: German text → `"de"`, English → `"en"`, empty/whitespace → `None`, very short
  → `None`. Deterministic across runs.
- `html_extract` / `pdf_extract`: returned dict now carries a detected `lang`; injected seams
  keep them offline.
- `pdf_extract` title chain: heading present → heading; heading absent + metadata title →
  cleaned metadata; both absent → `None`.
- `pdf_title.clean` / `pdf_title.from_url`: junk rejection; url-decode + separator handling;
  no-basename → `None`.
- `_upsert_document`: a title-less PDF doc gets a `from_url` title on `new`/`changed`; unchanged
  branch is untouched (that is the backfill's job).
- `storage.run_backfill` (seeded in-memory DB): populates `lang`/`final_url`/`title`;
  `final_url` picks the sha-matched full-fetch row over a later 304; `updated_at` is **not**
  changed; a second run is a no-op; `present=0` rows are skipped. Reuses the existing test-DB
  fixtures/patterns in `tests/test_storage_*.py`.

## Migration & dependencies

- **Schema**: none. All four target columns (`documents.lang`, `documents.final_url`,
  `documents.title`, `raw_docs.lang`) already exist. No `_migrate` change, no new index (Step A
  is index-free by design).
- **Dependency**: add `py3langid>=0.3` to `[project].dependencies`; `uv sync` picks it up. This
  is a Python wheel — it does **not** trigger a Rust rebuild.

## Operator runbook (the live pass is yours to run — see memory: prefers running crawls self)

On the machine holding the real `data/scraper.sqlite3` (4 GB) + `data/raw` cache, in the MSVC
shell (only needed for `uv sync`'s extension build, not for the backfill itself):

```powershell
uv sync --extra dev          # installs py3langid (rebuilds the unchanged extension harmlessly)
uv run pytest                # green before touching the corpus
# optional safety copy of the DB before the one-time write
uv run dhbw-scraper backfill # populates lang + final_url + titles; prints JSON counts
uv run dhbw-scraper stats    # sanity-check
uv run dhbw-scraper report -o data/analysis.html   # refresh the dashboard
```

Expected: `lang` populated on ~46k docs (with a NULL tail for thin/undetectable text),
`final_url` corrected on ~8k redirected docs, titles filled on ~1,097. Re-runnable safely.

## Files touched

- **New**: `src/scraper/lang.py`, `src/scraper/pdf_title.py`, tests for both + backfill.
- **Edit**: `src/scraper/html_extract.py`, `src/scraper/pdf_extract.py`,
  `src/scraper/storage.py` (`_upsert_document` filename fallback + `run_backfill`),
  `src/scraper/cli.py` (`backfill` command), `pyproject.toml` (`py3langid` dep),
  `README.md` / `CLAUDE.md` (document the `backfill` command).
- **Untouched**: all of `src/scrape-engine/` (Rust), the DB schema, the fetch/crawl path.
```
