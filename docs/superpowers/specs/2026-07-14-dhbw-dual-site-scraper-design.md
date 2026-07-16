# DHBW Dual-Site Scraper — Design

**Date:** 2026-07-14
**Status:** Approved design, pending implementation plan
**Supersedes:** the original single-site (`heidenheim.dhbw.de`), JSONL-output scraper in git history.

## Purpose

Stage 1 of a RAG pipeline: crawl two DHBW websites, download HTML pages and PDF
documents, extract clean structured content, and store a **quality-filtered corpus in
SQLite** for downstream chunking/indexing. The scraper is run **on a regular schedule**,
so it must detect what changed since the last run and hand the RAG layer a clean
**delta** (added / changed / removed), not the whole corpus each time. It must also be
**resumable**, **parallelizable**, and **observable** (live progress: done / queued /
kept / dropped).

## Scope

**In scope**

- Two target sites, each crawled strictly within its own hostname:
  - `heidenheim.dhbw.de` (seed: `https://www.heidenheim.dhbw.de/startseite`)
  - `www.dhbw.de` (seed: `https://www.dhbw.de`)
- Sitemap-seeded discovery **plus** in-domain link following.
- HTML and PDF download; HTML → trafilatura, PDF → Docling.
- Moderate quality filtering before insertion into the corpus.
- SQLite storage with a persistent queue, fetch audit log, extraction hand-off, and corpus.
- Two-phase pipeline (fetch, then extract), both resumable and parallel-safe.
- **Incremental re-crawl / change detection** across scheduled runs, producing an
  added/changed/unchanged/removed delta for downstream re-indexing.
- Informative CLI progress on plain stderr.

**Out of scope** (later RAG stages)

- Chunking, embedding, vector/search indexing.
- The scheduler itself (cron/systemd-timer is operational; the tool just exposes commands
  and a queryable delta).
- Cross-domain crawling into other DHBW locations (e.g. `mosbach.dhbw.de`). Links to
  other hostnames are recorded but **not** followed.
- OCR tuning beyond Docling defaults.

## Key decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| Target scope | Both sites, **strict per-domain** | Bounded, predictable; no crawl into the wider DHBW network |
| HTML extraction | **trafilatura** (markdown) | Main-content extraction strips TYPO3 nav/footer/menu boilerplate — critical for a clean RAG corpus |
| PDF extraction | **Docling** | Best-in-class layout/table/heading reconstruction for structure-heavy handbooks & regulations |
| Quality gate | **Moderate** | Drop empties, too-short, boilerplate/nav-only, exact duplicates, error pages |
| robots.txt | **Ignore** | Most PDFs live under disallowed `/fileadmin/...`; deliberate policy choice for authorized academic use, paired with polite delay + honest UA |
| Storage | **SQLite** (WAL) | Queryable, resumable, supports concurrent workers |
| Pipeline shape | **Two phases**: fetch → extract | Slow Docling never blocks the polite crawl; extraction is re-runnable without re-downloading |
| Change detection | **Full incremental** (conditional GET + content hash + sitemap `<lastmod>`) | Cheap re-runs, reliable deltas even when servers omit validators |
| Document identity | **Per URL** (stable across versions) | A page is "the same page" across content revisions; enables `updated_at` deltas + deletions |
| Progress UI | **plain stderr** | Dependency-light; TTY status line, plain lines when piped |

## Architecture

Evolves the original module layout (which was clean and well-bounded).

| Module | Responsibility | Change |
|---|---|---|
| `config.py` / `config.toml` | Config: **list of sites** + global politeness/db/worker settings | Extended (multi-site) |
| `fetch.py` | Polite HTTP with **conditional GET** (ETag/Last-Modified), content-type classification | Extended (validators, 304) |
| `links.py` | Cheap `<a href>` discovery + in-domain filtering (stdlib `HTMLParser`) | Extracted from old `crawl.py` |
| `crawl.py` | **Phase 1**: queue-driven fetch loop, change detection, link discovery, raw caching, hand-off | Rewritten |
| `html_extract.py` | trafilatura → title/text/markdown/metadata | Reused |
| `pdf_extract.py` | **Docling** → title/text/markdown/metadata | Rewritten (pdfplumber → Docling) |
| `quality.py` | Moderate accept/reject gate → `(accepted: bool, reason: str)` | New |
| `extract.py` | **Phase 2**: claim pending content, route to extractor, gate, upsert documents by URL | New |
| `storage.py` | SQLite schema, atomic queue/job claims, dedup, upserts, delta/stats queries | Rewritten (JSONL → SQLite) |
| `progress.py` | stderr progress helper (TTY status line / plain fallback) | New |
| `cli.py` | `fetch`, `extract`, `run`, `stats`, `delta` | Rewritten |

Each module has one clear job and a small interface, so it can be unit-tested in
isolation. Network (`fetch`) and the heavy extractor (`pdf_extract`) are isolated behind
function boundaries so the pipeline is testable offline.

## Data model (SQLite, WAL mode)

Four tables. `queue` is the canonical per-URL registry **and** the frontier **and** the
per-URL change-detection state (it persists across runs). `documents` is keyed by URL for
stable identity; `raw_docs` is keyed by content hash and doubles as the raw-bytes store
and the extraction cache.

```sql
-- Canonical per-URL registry + frontier + change-detection state. Persists across runs.
CREATE TABLE queue (
    url             TEXT PRIMARY KEY,
    site            TEXT NOT NULL,            -- 'heidenheim.dhbw.de' | 'www.dhbw.de'
    depth           INTEGER NOT NULL DEFAULT 0,
    discovered_from TEXT,                     -- parent url; NULL for seed/sitemap
    work_state      TEXT NOT NULL DEFAULT 'pending', -- pending|in_progress|done|error (this run)
    -- change detection --
    etag            TEXT,                     -- from last 200 response
    last_modified   TEXT,                     -- HTTP Last-Modified from last 200
    sitemap_lastmod TEXT,                     -- <lastmod> from sitemap, if any
    content_sha256  TEXT,                     -- hash of last fetched body
    http_status     INTEGER,                  -- last status (200/304/404/...)
    present         INTEGER NOT NULL DEFAULT 1,-- 1 = live, 0 = removed (404/410/gone)
    first_seen_at   TEXT NOT NULL,
    last_checked_at TEXT,                     -- last time we hit the network for it
    last_changed_at TEXT                      -- last time its content_sha256 changed
);
CREATE INDEX idx_queue_state ON queue(work_state);
CREATE INDEX idx_queue_present ON queue(present);

-- Append-only audit of every fetch attempt, tagged with the run it belongs to.
CREATE TABLE crawl_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,               -- groups a single scheduled run
    url          TEXT NOT NULL,
    final_url    TEXT,
    site         TEXT,
    status       INTEGER,                     -- HTTP status (0 = network error, 304 = unchanged)
    content_type TEXT,
    sha256       TEXT,
    bytes        INTEGER,
    kind         TEXT,                        -- html|pdf|other
    outcome      TEXT,                        -- new|changed|unchanged|removed|error|skipped
    error        TEXT,
    fetched_at   TEXT NOT NULL
);
CREATE INDEX idx_crawl_log_run ON crawl_log(run_id);

-- Content-addressed raw-bytes store AND extraction cache. One row per unique content blob.
CREATE TABLE raw_docs (
    content_sha256 TEXT PRIMARY KEY,          -- dedup identical bytes across URLs
    source_type    TEXT NOT NULL,             -- html|pdf
    raw_path       TEXT NOT NULL,             -- content-addressed file under data/raw/
    bytes          INTEGER NOT NULL,
    first_seen_at  TEXT NOT NULL,
    extract_state  TEXT NOT NULL DEFAULT 'pending', -- pending|in_progress|done|error|rejected
    -- cached extraction result (so identical bytes are extracted exactly once) --
    title          TEXT,
    text           TEXT,
    markdown       TEXT,
    lang           TEXT,
    word_count     INTEGER,
    metadata       TEXT,                      -- JSON
    quality_ok     INTEGER,                   -- 1 accepted, 0 rejected by gate
    reject_reason  TEXT,
    extract_error  TEXT,
    extracted_at   TEXT
);
CREATE INDEX idx_raw_extract_state ON raw_docs(extract_state);

-- Clean RAG corpus, keyed by URL for stable identity across content revisions.
CREATE TABLE documents (
    id             TEXT PRIMARY KEY,          -- sha1(url)[:16], stable per URL
    url            TEXT NOT NULL UNIQUE,
    final_url      TEXT,
    site           TEXT NOT NULL,
    source_type    TEXT NOT NULL,             -- html|pdf
    content_sha256 TEXT NOT NULL,             -- current version -> raw_docs
    title          TEXT,
    text           TEXT NOT NULL,             -- plain text (denormalized from raw_docs)
    markdown       TEXT NOT NULL,             -- structured, for RAG chunking
    lang           TEXT,
    word_count     INTEGER NOT NULL,
    metadata       TEXT,                      -- JSON
    present        INTEGER NOT NULL DEFAULT 1,-- mirrors queue.present (0 = deleted upstream)
    revision       INTEGER NOT NULL DEFAULT 1,-- bumped each time content changes
    first_indexed_at TEXT NOT NULL,
    updated_at     TEXT NOT NULL              -- bumped on every content change  <-- re-index key
);
CREATE INDEX idx_documents_site ON documents(site);
CREATE INDEX idx_documents_updated ON documents(updated_at);
CREATE INDEX idx_documents_present ON documents(present);
```

Dedup / identity summary:

- **URL dedup:** `queue.url` PK (`INSERT OR IGNORE` when enqueuing).
- **Raw-bytes + extraction dedup:** `raw_docs.content_sha256` PK — identical bytes are
  stored and Docling-extracted **once**, even if reached via several URLs.
- **Corpus identity:** `documents` keyed by URL. A changed page updates its existing row
  in place (new `content_sha256`, `revision+1`, new `updated_at`) — the RAG layer sees a
  changed document, not a brand-new one. Identical content served at two different URLs
  yields two `documents` rows (correct — they are two pages) but only one extraction pass
  (cache hit on `raw_docs`).

## Change detection & incremental re-crawl

Every run gets a `run_id`. Cheap → expensive layering:

1. **Sitemap `<lastmod>` shortlist.** Re-fetch each site's sitemap; update
   `queue.sitemap_lastmod`. New URLs are inserted `pending`. Known URLs whose `<lastmod>`
   advanced are marked `pending`. This alone avoids re-checking thousands of untouched URLs.
2. **Conditional GET.** When (re)fetching a known URL, send `If-None-Match: <etag>` and/or
   `If-Modified-Since: <last_modified>` from `queue`. A **`304 Not Modified`** ends the
   work with no body downloaded: set `http_status=304`, `last_checked_at=now`,
   `outcome='unchanged'`. Ideal for `/fileadmin/` **PDFs** (static, well-behaved validators).
3. **Content hash.** On a `200`, hash the body. If it equals `queue.content_sha256` →
   `outcome='unchanged'` (update validators only). If different → `outcome='changed'`
   (new URL → `'new'`): update `content_sha256`, `last_changed_at=now`, refresh validators,
   and upsert/refresh the `raw_docs` row so extraction re-runs. Catches changes even on
   dynamic TYPO3 HTML that omits trustworthy validators.

**Deletions.** A known, previously-present URL that returns `404`/`410` is marked
`present=0` (`outcome='removed'`), and its `documents` row is set `present=0` so the RAG
layer drops it. (Dropping-from-sitemap is treated as a soft signal only; HTTP status is the
authoritative deletion signal to avoid deleting pages that merely fell out of a sitemap.)

**Re-check policy** (config): by default a scheduled run re-checks all `present=1` URLs
(cheap thanks to conditional GET) plus everything new/changed from the sitemap. A
`--changed-only` flag restricts to sitemap-`<lastmod>` candidates for the fastest possible
run; `--full` ignores validators and forces re-download (e.g. after an extractor upgrade).

**The delta for re-indexing.** Downstream reads exactly what changed since its last index
run — no full-corpus rescan:

```sql
-- upserts to (re)embed:
SELECT * FROM documents WHERE updated_at > :last_index_run AND present = 1;
-- ids to delete from the index:
SELECT id, url FROM documents WHERE present = 0 AND updated_at > :last_index_run;
```

`dhbw-scraper delta --since <ISO>` prints these two sets (JSON) for the indexer to consume.

## Phase 1 — fetch/download (`crawl.py`)

Per configured site, refresh the sitemap into `queue` (step 1 above), then seed the seed
URL. Reset any stale `in_progress` rows (from a crash) to `pending`.

Worker loop (one worker per host for politeness; hosts progress in parallel):

1. **Claim** next `pending` row atomically (`BEGIN IMMEDIATE` → `UPDATE ... work_state=
   'in_progress' ... RETURNING url` → `COMMIT`).
2. Enforce per-host `request_delay_seconds` since this worker's last request.
3. **Conditional** `fetch()` using stored `etag`/`last_modified`; record in `crawl_log`.
4. Branch on result:
   - **304** → `outcome='unchanged'`; update `last_checked_at`; done.
   - **200**, hash unchanged → `outcome='unchanged'`; refresh validators; done.
   - **200**, hash changed/new → `outcome='changed'|'new'`; update validators,
     `content_sha256`, `last_changed_at`; write raw file if not already cached; upsert
     `raw_docs` (`extract_state='pending'`). For **HTML**, discover links and enqueue
     in-domain ones (`INSERT OR IGNORE`, `depth+1`). **PDFs** are leaf nodes.
   - **404/410** → `present=0`, `outcome='removed'`; mark `documents.present=0`.
   - other errors → `work_state='error'`, logged.
5. Bounded by `max_pages` (0 = unlimited). `kind == other` (images/css/…) is skipped.

## Phase 2 — extract (`extract.py`)

Parallel pool of N workers (N ≈ CPU cores; Docling is CPU-bound):

1. **Claim** a `raw_docs` row where `extract_state='pending'` (atomic).
2. Route by `source_type`: `html` → trafilatura, `pdf` → Docling → title/text/markdown/metadata.
3. Run the **quality gate**. Cache the outcome on the `raw_docs` row (`title`, `text`,
   `markdown`, `word_count`, `lang`, `metadata`, `quality_ok`, `reject_reason`),
   `extract_state='done'` (or `'rejected'`/`'error'`).
4. **Materialize documents:** for every `present=1` URL in `queue` whose current
   `content_sha256` equals this row and whose extraction is `quality_ok`, **upsert** the
   per-URL `documents` row: insert if new (`revision=1`, set `first_indexed_at`), else on a
   content change bump `revision`, refresh text/markdown/metadata, set `updated_at=now`.
   Rejected extractions do not create a `documents` row; if a URL that already had a
   document now extracts to rejected content, the existing row is left as-is and the
   rejection is flagged in `crawl_log`.

Re-runnable: reset `raw_docs.extract_state` to `pending` to re-extract after a gate or
Docling change — no re-download.

## Quality gate (moderate) — `quality.py`

`evaluate(doc) -> (accepted: bool, reason: str)`. Reject when:

- extraction returned nothing / empty text;
- `word_count < min_words` (default **50**, configurable);
- **boilerplate/nav-only**: markdown is almost entirely links (link-token ratio above a
  threshold) or is just a bare title with no body.

Exact-duplicate **content** is handled structurally (one `raw_docs`/extraction per hash),
not in the gate. Every rejection reason is recorded on `raw_docs` and surfaced in progress
output, so dropped content is auditable, never silently lost.

## CLI & progress (`cli.py`, `progress.py`)

Commands:

- `dhbw-scraper fetch [--max-pages N] [--changed-only|--full] [--workers-per-host 1]` — Phase 1.
- `dhbw-scraper extract [--workers N]` — Phase 2.
- `dhbw-scraper run [...]` — fetch then extract (the normal scheduled entrypoint).
- `dhbw-scraper stats` — queue depth by state, docs by site/type, present vs removed,
  rejects by reason, last run summary.
- `dhbw-scraper delta --since <ISO>` — emit the re-index delta (upserts + deletions) as JSON.

Live progress on **stderr**:

- Per-site header: `── Crawling www.dhbw.de — 1,204 sitemap URLs (312 new / 87 changed) ──`.
- Running status line (TTY): `[www.dhbw.de] ✓142 checked | 89 unchanged | 41 changed | 12 new | queue 389 | -3 removed → /studium/bewerbung`.
- Dropped items show reason: `↳ dropped (too short: 12 words)`.
- Piped/non-TTY output falls back to periodic plain lines (clean logs).
- Final per-run summary: checked / new / changed / unchanged / removed / kept /
  dropped-by-reason / errors, elapsed time, DB path.

## Configuration (`config.toml`)

```toml
[[sites]]
name = "heidenheim"
seed_url = "https://www.heidenheim.dhbw.de/startseite"
allowed_domain = "heidenheim.dhbw.de"

[[sites]]
name = "dhbw"
seed_url = "https://www.dhbw.de"
allowed_domain = "www.dhbw.de"

[crawl]
use_sitemap = true
max_pages = 0                 # 0 = unlimited
request_delay_seconds = 1.0   # per host
respect_robots = false
workers_per_host = 1
recheck = "all"               # "all" | "changed-only"  (default for scheduled runs)

[extract]
workers = 4
min_words = 50

[storage]
db_file = "data/scraper.sqlite3"
raw_dir = "data/raw"
```

`user_agent`: honest, configurable, includes a contact address so DHBW admins can reach
out; defaults to a value naming the project + contact.

## Testing (TDD)

Offline unit tests, extractor/network boundaries stubbed:

- `quality`: accept/reject for each rule (empty, short, nav-only, good).
- `storage`: schema init; atomic queue claim; URL dedup; content dedup; per-URL document
  **upsert** (insert vs. revision-bump); `present` flip on deletion; delta query; stats;
  `in_progress` reset on restart.
- `change-detection` seam: 304 → unchanged; 200 same-hash → unchanged; 200 new-hash →
  changed (revision bump, `updated_at` advances); 404 → removed (`present=0` on doc);
  sitemap `<lastmod>` shortlisting.
- `links`: `<a href>` discovery, in-domain filtering, `mailto:`/`tel:`/fragment handling.
- `html_extract`: fixture HTML → expected markdown/title/metadata; boilerplate stripped.
- `pdf_extract`: small fixture PDF → non-empty structured text (Docling boundary; kept
  minimal / mockable so tests stay fast and offline).
- `fetch`: conditional-request headers set from stored validators; 304 handling.
- `delta`: since-timestamp returns correct upserts + deletions.

## Dependencies & environment

- `trafilatura` (HTML), `docling` (PDF), stdlib `sqlite3`, `urllib`, `HTMLParser`.
- **Docling caveat:** pulls `torch` + downloads model weights on first run — a heavier
  NixOS build than the old pdfplumber. README documents `nix develop && uv sync` plus the
  first-run model download; note the native-wheel fallback via nixpkgs if `uv` struggles.
- No `rich`/heavy CLI deps — progress is plain stderr.

## Risks / open considerations

- **SQLite write contention:** a handful of workers with short claim transactions in WAL
  mode is fine; not a high-throughput job queue. If contention appears, reduce workers or
  add brief retry-on-busy.
- **Validator trustworthiness:** TYPO3 HTML may send weak/absent `ETag`/`Last-Modified`;
  the content-hash layer is the safety net, so correctness never depends on validators.
- **Docling on NixOS:** first-run model download + torch size; documented, with nixpkgs
  fallback.
- **`www.dhbw.de` scale:** unknown page count; `max_pages`, the persistent queue, and
  `--changed-only` keep partial/scheduled runs safe and cheap.
