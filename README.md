# DHBW Multi-Site Scraper

Stage 1 of a RAG pipeline: incrementally crawl the **DHBW web presence**, extract clean
text, and store everything in a local **SQLite** database ready for the next stages
(chunking, then a search index — both out of scope here).

Sites (see [`config.toml`](./config.toml) for the authoritative list) — the central
portal plus all nine campus locations and the Center for Advanced Studies (CAS):

| Site | Seed | `allowed_domain` |
| --- | --- | --- |
| `dhbw` (central portal) | <https://www.dhbw.de> | `www.dhbw.de` |
| `heidenheim` | <https://www.heidenheim.dhbw.de/startseite> | `heidenheim.dhbw.de` |
| `mannheim` | <https://www.mannheim.dhbw.de> | `mannheim.dhbw.de` |
| `stuttgart` | <https://www.dhbw-stuttgart.de> | `dhbw-stuttgart.de` |
| `karlsruhe` | <https://www.karlsruhe.dhbw.de> | `karlsruhe.dhbw.de` |
| `mosbach` | <https://www.mosbach.dhbw.de> | `mosbach.dhbw.de` |
| `heilbronn` | <https://www.heilbronn.dhbw.de> | `heilbronn.dhbw.de` |
| `ravensburg` | <https://www.ravensburg.dhbw.de/startseite> | `ravensburg.dhbw.de` |
| `loerrach` | <https://dhbw-loerrach.de/home> | `dhbw-loerrach.de` |
| `villingen_schwenningen` | <https://www.dhbw-vs.de> | `dhbw-vs.de` |
| `cas` | <https://www.cas.dhbw.de> | `cas.dhbw.de` |

Each site is crawled strictly in-domain — links leaving `allowed_domain` are recorded but
never followed or fetched. `allowed_domain` deliberately omits the `www.` prefix:
`in_domain()` matches the bare host and any subdomain, so `mannheim.dhbw.de` also covers
`www.mannheim.dhbw.de`. Note that Stuttgart, Lörrach, and Villingen-Schwenningen do **not**
follow the `<location>.dhbw.de` pattern. Karlsruhe and CAS expose no `/sitemap.xml`, so
they are discovered by in-domain link-crawling alone.

## What it does

The pipeline runs in two phases, both re-runnable at any time for incremental re-crawls:

1. **Fetch** (`dhbw-scraper fetch`) — discover URLs from each site's sitemap plus a
   focused in-domain link crawl, then download them. Already-known URLs are re-checked
   with conditional GET (`If-None-Match` / `If-Modified-Since`); unchanged pages come
   back as `304` and are skipped cheaply. New/changed bytes are cached under
   `data/raw/<sha256>.<ext>` and queued for extraction.
2. **Extract** (`dhbw-scraper extract`) — convert cached HTML/PDF bytes into clean text:
   HTML via [`trafilatura`](https://github.com/adbar/trafilatura), PDF via
   [PyMuPDF4LLM](https://pymupdf.readthedocs.io/en/latest/pymupdf4llm/). Each result passes
   a moderate quality gate (below) before it is materialized into the `documents` table.

`dhbw-scraper run` does fetch then extract in one command.

Progress is reported live on **stderr** as the run proceeds: a header line per phase
(`── Crawling ──` for fetch, `── Extracting ──` for extract), then a live status block —
one row per site with that site's counts and current URL, plus a `TOTAL` row summing all
sites and a throughput figure (`… | 24/s`). On a TTY the block is repainted in place on a
throttled tick (so many workers no longer cause flicker or lag); when piped/redirected it
is emitted as periodic plain snapshot lines. Dropped items (e.g. quality-gate rejects) get
a note line, and each site/phase prints a final summary line. The machine-readable final
counts are still printed on **stdout**.

## Architecture: Rust Phase 1, Python Phase 2

**Phase 1 (fetch/crawl) is implemented in Rust** (`src/scrape-engine/`, exposed to Python as the
`scraper._engine` extension via [PyO3](https://pyo3.rs) + built with
[maturin](https://www.maturin.rs)). It is a `tokio` async crawler with a **single dedicated
SQLite writer task** fed by an in-memory frontier, so fetch workers run lock-free and there
is no write-lock contention — the crawl parallelises cleanly across all sites and workers.
It owns every Phase-1 write to the SQLite DB; `src/scraper/crawl.py` is now a thin
adapter that forwards `run_fetch` to the extension.

**Phase 2 (extract) stays pure Python** (trafilatura / PyMuPDF4LLM) and reads the exact same
SQLite database and `data/raw/<sha256>.<ext>` cache the Rust engine wrote.

## Setup

Building the extension needs a **Rust toolchain** and a **C compiler** (for `rusqlite`'s
bundled SQLite); reqwest uses rustls, so no system OpenSSL is required.

### NixOS

```sh
nix develop          # python3.14 + uv + rustc/cargo/maturin + git
uv sync              # install Python deps (trafilatura, pymupdf4llm, maturin) into .venv
uv run maturin develop --release   # build the Rust Phase-1 extension into .venv
```

### Windows

Install [rustup](https://rustup.rs) (the `x86_64-pc-windows-msvc` toolchain) and the
**Visual Studio Build Tools** with the *Desktop development with C++* workload (this also
provides the Windows SDK that `rusqlite`'s bundled SQLite needs).

The Rust build needs the MSVC compiler (`cl.exe`/`link.exe`) on `PATH`. Open the **"x64
Native Tools Command Prompt for VS 2022"** (installed with the Build Tools) — it has that
environment preloaded — and build from there:

```powershell
uv sync --extra dev            # installs deps AND builds the extension into .venv
uv run pytest                  # 143 tests, incl. the end-to-end crawl test in tests/test_engine_run_fetch.py
```

`uv sync` builds the extension as part of installing the project, so it must run in that
MSVC-enabled shell — a plain shell fails with `cl.exe not found`. Use `--extra dev` to also
get `pytest` + `maturin`. After changing Rust code, rebuild with
`uv run maturin develop --release` (or plain `maturin develop` for a faster debug build) in
the same shell.

> The pyo3-abi3 forward-compatibility flag needed to build against CPython 3.14 is set for
> you in [`.cargo/config.toml`](./.cargo/config.toml), so you don't need any extra env vars.

The Rust test binaries link libpython, so running them needs the interpreter's
`python3.dll` on `PATH` in addition to the MSVC environment. From the same Native Tools
prompt:

```powershell
$env:PYO3_PYTHON = "$PWD\.venv\Scripts\python.exe"
$env:Path = "$(& .venv\Scripts\python.exe -c 'import sys; print(sys.base_prefix)');$env:Path"
cargo test
```

(`uv sync` and `maturin develop` don't need this — the host CPython provides the symbols
there. Only the standalone `cargo test` binaries do.)

If you use direnv on NixOS, `direnv allow` auto-enters the `nix develop` shell (which already
has rustc/cargo/maturin) on `cd`.

PDF extraction uses **PyMuPDF4LLM**, which is lightweight (no `torch`, no ML models). There
is no model download — the extractor works offline right after install, with no first-PDF
delay.

> **Native-wheel fallback:** if a compiled dependency (e.g. `lxml`, pulled in by
> `trafilatura`) misbehaves under `uv` on NixOS, switch the flake to a Nix-built interpreter
> with the packages pre-built — `trafilatura` and `lxml` are both in nixpkgs
> (`python312.withPackages (ps: [ ps.trafilatura ])`). `pymupdf` ships prebuilt
> manylinux/Windows wheels, so it usually installs cleanly under `uv`.

### Contributing

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for the commit convention (Conventional Commits)
and the git hooks (lint/format on commit, tests on push). Install them once after `uv sync`:

```sh
uv run pre-commit install --install-hooks
```

## Usage

```sh
# Small bounded run to sanity-check things end to end:
uv run dhbw-scraper run --max-pages 5

# Inspect what landed in the database:
uv run dhbw-scraper stats

# Get everything changed since a point in time (for feeding the next pipeline stage):
uv run dhbw-scraper delta --since 2026-07-01T00:00:00
```

Run phases separately when useful (e.g. re-extract without re-fetching):

```sh
uv run dhbw-scraper fetch --max-pages 5   # phase 1: crawl + download
uv run dhbw-scraper extract --workers 4   # phase 2: extract + quality-gate
```

Note that `--max-pages` (and `crawl.max_pages` in `config.toml`) is a **per-site** budget, not a
global cap: with two `[[sites]]` configured, `--max-pages 5` allows up to 5 new pages from
*each* site, not 5 total.

Distinct from the per-site budget, `crawl.max_pages_per_host` (default `50000`, `0` =
unlimited) caps how many pages any **single hostname** may contribute. It is a
defense-in-depth backstop against a runaway subdomain — e.g. a booking or calendar
webapp whose URLs explode combinatorially — monopolizing a site's crawl the way the
`buchen.dhbw-vs.de` Meeting Room Booking System once did (~940k permutations in one run).
No legitimate campus host approaches 50,000 pages; a spider trap blows straight past it
and is cut off, with the skipped count reported in the run summary (never silently
truncated). Known traps (`buchen.*`, `moodle.*`, `elearning.*`, Solr search, …) are
denylisted outright in `src/scrape-engine/links.rs`; the per-host cap only catches *unknown* ones.

`fetch` and `run` also accept:

- `--site NAME` — crawl only the named site(s), matched by config `name` **or**
  `allowed_domain` (repeatable). Scopes both the sitemap refresh and the crawl to the
  selected site(s), leaving the others untouched — use it to re-crawl one campus in
  isolation.
- `--max-pages-per-host N` — override `crawl.max_pages_per_host` for this run (`0` =
  unlimited).
- `--workers-per-host N` — concurrent fetch workers per host, overriding
  `crawl.workers_per_host` from `config.toml` for this invocation. Each host still shares
  one `request_delay_seconds` rate limiter across its workers, so raising this speeds up
  a run without hammering any single site.
- `--request-delay SECONDS` — per-host delay between requests, overriding
  `crawl.request_delay_seconds`. Raise it (and lower `--workers-per-host`) to crawl a
  server politely and avoid connection-level failures.
- `--changed-only` — restrict re-checks to URLs the sitemap `<lastmod>` flags as changed
  (same as `recheck = "changed-only"` in config, for one run).
- `--full` — re-check every present URL and ignore stored `ETag`/`Last-Modified`
  validators, forcing a full re-download.
- `--new-only` — fetch only queued URLs never fetched before (and everything they link
  to); never re-download a page already in the store (same as `recheck = "new-only"`).

`--changed-only`, `--full`, and `--new-only` are mutually exclusive. With none of them,
the command falls back to `crawl.recheck` from `config.toml`.

All commands accept a top-level `--config PATH` to point at a `config.toml` other than
the one discovered by walking up from the current directory.

### Re-crawling a single site from scratch

When a site's stored crawl is spoilt — e.g. an old run drowned in a spider trap and never
reached the real content — reset just that site and re-crawl it in isolation:

```sh
# 1. Delete the site's queue / crawl_log / documents / links rows so it re-seeds clean.
#    The content-addressed raw_docs cache is kept, so unchanged pages are not re-extracted.
uv run dhbw-scraper reset-site --site villingen_schwenningen

# 2. Re-crawl ONLY that site, politely. On a fresh reset every row is new, so the default
#    recheck=new-only crawls the whole site. Known traps (buchen.*, …) are blocked and the
#    per-host cap is a backstop; --request-delay + a lower --workers-per-host avoid the
#    connection failures an aggressive crawl provokes on a small institutional server.
uv run dhbw-scraper fetch --site villingen_schwenningen --workers-per-host 4 --request-delay 0.5

# 3. Extract the new content, then confirm the recovery.
uv run dhbw-scraper extract
uv run dhbw-scraper stats
```

`reset-site` takes `--site` (config name or `allowed_domain`, repeatable) and is the only
destructive command; it prints the per-table delete counts.

### Rebuilding the link graph (`backfill-links`)

The `links` edge table is written **only on a full-body 2xx fetch**: when a page comes
back `304 Not Modified` (the common case on any re-crawl once its `ETag`/`Last-Modified`
are stored), no edges are emitted. So a page fetched once and thereafter only revalidated
keeps whatever edges that first full fetch wrote — and pages first crawled before edge
recording existed have none at all. The result is a link graph far sparser than the corpus.

`backfill-links` repairs this **without re-downloading anything**. It re-reads the
content-addressed raw HTML already in `raw_dir`, re-runs the exact same link discovery the
crawler uses, and inserts any missing edges:

```sh
uv run dhbw-scraper backfill-links
# -> pages=45996 edges=812340 raw_missing=0
```

- **No network.** It only reads local raw blobs; nothing is fetched.
- **Additive and idempotent.** Edges are `INSERT OR IGNORE` on `(src_url, dst_url)`, so
  re-running never duplicates and only fills gaps. `edges` in the summary counts rows newly
  inserted (0 on a second run), `pages` the HTML pages re-parsed, and `raw_missing` any page
  whose blob was absent on disk (skipped, never fatal).
- **Scope.** Only present (`present=1`) pages whose stored content is HTML are processed;
  PDFs never carry edges, and **errored pages have no raw blob**, so neither is covered —
  errored URLs still need a re-fetch to ever gain edges. Relative links resolve against each
  page's stored URL rather than the live post-redirect `final_url`, which differs only for
  the handful of pages that were redirected.

Run tests:

```sh
uv run pytest
```

## Change detection

Re-running `fetch` is cheap and safe. How much gets re-checked is controlled by
`crawl.recheck` (or the `--changed-only`/`--full` flags, see above):

- `recheck = "all"` (default) — every already-present URL is re-checked each run.
- `recheck = "changed-only"` — only URLs the sitemap `<lastmod>` scan flags as candidates
  are re-checked; the rest are left alone until they show up as changed.
- `recheck = "new-only"` — fetch only queued URLs never fetched before; a page already
  in the store is never re-downloaded even if a change signal fires. Newly-discovered
  links are still followed, so the crawl cascades forward into all new pages. Use this to
  drain remaining un-fetched work after a partial crawl without re-checking the corpus.

Either way, a re-check is a conditional GET:

- Every re-checked URL sends its stored `ETag`/`Last-Modified` validators; a `304`
  response short-circuits to `unchanged` with no download.
- If the server doesn't support conditional GET, the downloaded body is hashed
  (SHA-256) and compared against the previously stored hash — identical content is
  still treated as `unchanged`.
- Sitemaps are re-scanned on every `fetch` and their `<lastmod>` timestamps drive
  re-queueing: a URL whose sitemap `lastmod` has advanced is reset to `pending` even if
  its HTTP validators didn't trip.
- `--full` skips sending the stored validators altogether, so every re-checked URL is
  downloaded fresh regardless of whether the server would have answered `304`.
- A `404`/`410` response marks the URL (and its materialized document, if any) as
  removed (`present = 0`) rather than deleting it — so `delta` can report the deletion
  to downstream consumers.
- New raw content is deduplicated by content hash (`raw_docs.content_sha256`): if two
  URLs (or a URL re-appearing after removal) share identical bytes, extraction runs
  once and both URLs get their `documents` row(s) materialized from it.

## Quality gate

An extracted document is accepted into `documents` only if:

- it produced non-empty text, and
- it has at least `extract.min_words` words (default `50`, see `config.toml`), and
- it isn't dominated by navigation — rejected if the words inside Markdown link anchors
  exceed 60% of its words (link indexes, menu/footer link bars), and
- it isn't a short page dominated by cookie-consent, login-wall, or error/empty-state
  boilerplate (matched conservatively: only short pages carrying several such phrases).

Rejected/errored extractions stay recorded in `raw_docs` (with `reject_reason` /
`extract_error`) but never reach `documents`.

## robots.txt policy

`robots.txt` is **deliberately not consulted** — `crawl.respect_robots` exists in
`config.toml` only for auditability/future use and currently has no effect either way
(unlike `workers_per_host` and `recheck`, which are both fully wired up — see above).
In its place, the scraper identifies itself honestly and contactably via a configurable
`user_agent` string:

```toml
user_agent = "dhbw-scraper/0.2 (+https://github.com/deadmade/Integrationsseminar; contact: CONTACT_EMAIL)"
```

**Fill in a real contact address before running this against the live sites.** Crawl
politely regardless: keep `request_delay_seconds` and `workers_per_host` at sane values
for a small institutional site.

## Storage: SQLite schema

Everything lives in one SQLite database (`storage.db_file` in `config.toml`, default
`data/scraper.sqlite3`), opened in WAL mode with `BEGIN IMMEDIATE` for atomic work
claims so `fetch`/`extract` can run with multiple workers safely. Four tables:

| Table | Purpose |
|---|---|
| `queue` | The crawl frontier and per-URL state: site, depth, `work_state` (`pending`/`in_progress`/`done`/`error`), HTTP validators (`etag`, `last_modified`), `sitemap_lastmod`, the last known `content_sha256`, and `present` (0 once a URL 404s). |
| `crawl_log` | Append-only log of every fetch attempt, one row per `(run_id, url)` attempt: status, content-type, hash, byte count, and an `outcome` (`new`/`changed`/`unchanged`/`removed`/`error`/`skipped`). |
| `raw_docs` | Content-addressed cache of downloaded bytes (`content_sha256` primary key), pointing at the file under `data/raw/`, plus the phase-2 extraction result (`title`, `text`, `markdown`, `word_count`, `metadata`) and its `extract_state`/`quality_ok`/`reject_reason` once processed. One row per unique content blob, however many URLs share it. |
| `documents` | The materialized corpus: one row per URL, carrying the current extracted content, a monotonically increasing `revision` bumped on content change, `present` (0 once its URL/content is removed), and `updated_at`/`first_indexed_at` timestamps that `delta` filters on. |
| `links` | Outbound link graph: one row per `(src_url, dst_url)` edge for **every** `<a href>` a crawled page emits — in-domain *and* external/cross-campus. `in_domain = 1` marks a follow candidate; external edges are recorded but never crawled. Purely additive graph data (no Phase-2 query reads it); `queue.discovered_from` still records the first discoverer for back-compat. |

## `delta` for downstream re-indexing

```sh
uv run dhbw-scraper delta --since 2026-07-01T00:00:00
```

Prints JSON with `upserts` (documents changed/added since the timestamp, `present = 1`)
and `deletions` (documents removed since the timestamp), so a downstream indexer can
stay in sync without re-processing the whole corpus each time.

## Data layout

| Path | Committed? | Contents |
|---|---|---|
| `data/scraper.sqlite3` (+ `-wal`/`-shm`) | no (gitignored) | the database described above |
| `data/raw/<sha256>.<ext>` | no (gitignored) | exact bytes as downloaded — content-addressed, re-fetchable cache |
| `data/.gitkeep` | yes | keeps the (otherwise gitignored) `data/` directory tracked in git |

## Configuration

All knobs live in [`config.toml`](./config.toml): the two `[[sites]]` (`seed_url`,
`allowed_domain`), `[crawl]` (sitemap use, `max_pages`, `request_delay_seconds`,
`respect_robots`, `workers_per_host`, `recheck`, `user_agent`), `[extract]` (`workers`,
`min_words`), and `[storage]` (`db_file`, `raw_dir`). `workers_per_host` and `recheck`
can be overridden per-run without editing the file via the `fetch`/`run` flags
`--workers-per-host`, `--changed-only`, and `--full` (see [Usage](#usage) and
[Change detection](#change-detection) above).

## Project layout

```
src/scraper/         Phase 2 + CLI (pure Python)
  __init__.py      empty package marker
  config.py        load + validate config.toml
  storage.py       SQLite schema (incl. links), Phase-2 claims/upserts/delta, raw-file cache
  fetch.py         content-type classification + ext_for (used by Phase 2 extraction)
  crawl.py         phase 1 adapter -> scraper._engine.run_fetch (Rust engine)
  html_extract.py  trafilatura -> markdown + metadata
  pdf_extract.py   PyMuPDF4LLM -> markdown/text (lazy import, lightweight)
  markdown.py      shared markdown -> plain-text stripper (keeps word_count consistent across HTML/PDF)
  quality.py       moderate quality gate (min words, nav ratio, login/cookie/error filters)
  extract.py       phase 2: extract, quality-gate, materialize documents
  progress.py      stderr progress reporting (TTY status line / plain log lines)
  dashboard.py     self-contained read-only HTML analysis report (backs `report`)
  cli.py           `fetch` / `extract` / `run` / `stats` / `delta` entrypoints
  __main__.py      `python -m scraper` entry point -> cli.main
  _engine.pyd      built Rust extension (scraper._engine), gitignored

src/scrape-engine/   Phase-1 crawler (compiled to scraper._engine via maturin/PyO3)
  crawl.rs          orchestrator: frontier, per-host workers, rate limit, termination
  writer.rs         single SQLite writer + in-memory frontier (no write contention)
  fetch.rs          reqwest conditional-GET HttpClient + content-type classify
  links.rs          <a href> discovery, in-domain filter, crawler-trap rules
  sitemap.rs        sitemap + nested sitemap-index discovery
  storage.rs        SQLite schema + write ops + content-addressed raw cache
  {config,outcome,progress,lib}.rs  config mapping, change detection, progress, PyO3

Cargo.toml           root manifest: [lib] path -> src/scrape-engine/lib.rs
Cargo.lock           pinned dependency versions (checked in for reproducible builds)
tests/               pytest suites + fixtures/
  scrape-engine/     links/sitemap parity + end-to-end orchestration (cargo)
```
