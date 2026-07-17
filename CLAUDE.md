# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`dhbw-scraper` is **stage 1 of a RAG pipeline**: incrementally crawl the DHBW web
presence (central portal + 9 campuses + CAS, see [`config.toml`](./config.toml)),
extract clean text, and store everything in one local SQLite DB for downstream
stages. [`README.md`](./README.md) is the authoritative, detailed spec for behavior
(change detection, quality gate, CLI flags, schema) — read it for anything not
covered here.

## Architecture: Rust Phase 1, Python Phase 2

The pipeline is two re-runnable phases sharing one SQLite DB + one content-addressed
raw cache (`data/raw/<sha256>.<ext>`):

- **Phase 1 — fetch/crawl — is Rust** (`src/scrape-engine/`), compiled to the Python extension
  `scraper._engine` via [PyO3](https://pyo3.rs) + [maturin](https://www.maturin.rs).
  It's a `tokio` async crawler with a **single dedicated SQLite writer task** fed by an
  in-memory frontier, so fetch workers are lock-free and there's no write-lock
  contention. It owns *every* Phase-1 DB write. [`crawl.py`](src/scraper/crawl.py)
  is a thin adapter that forwards `run_fetch` to the extension — do not
  reimplement crawl logic in Python.
- **Phase 2 — extract — is pure Python** (trafilatura for HTML, PyMuPDF4LLM for PDF).
  It reads the same DB + raw cache the Rust engine wrote.

The Rust modules mirror the pipeline: `crawl.rs` (orchestrator), `writer.rs` (the sole
SQLite writer + frontier), `fetch.rs` (reqwest conditional-GET), `links.rs` (link
discovery, in-domain filter, **crawler-trap denylist**), `sitemap.rs`, `storage.rs`,
`lib.rs` (PyO3 boundary). `tests/scrape-engine/` holds `links_parity`,
`sitemap_parity`, and end-to-end `orchestration` tests.

### SQLite storage model

One DB (`data/scraper.sqlite3`, WAL mode, `BEGIN IMMEDIATE` for atomic multi-worker
work claims). Five tables — `queue` (frontier + per-URL state), `crawl_log`
(append-only fetch attempts), `raw_docs` (content-addressed byte cache + extraction
result, keyed by `content_sha256`), `documents` (materialized corpus, one row per URL
with a `revision`), and `links` (outbound edge graph). See the README "Storage" section
for column-level detail.

## Build & toolchain (the main gotcha)

Building the extension needs a **Rust toolchain + a C compiler** (for `rusqlite`'s
bundled SQLite). `uv sync` itself compiles the extension, so it must run in an
MSVC-enabled shell on Windows.

**Windows** (primary dev platform here) — run everything from the **"x64 Native Tools
Command Prompt for VS 2022"**, which has the MSVC env preloaded:
```powershell
uv sync --extra dev            # installs deps AND builds the _engine extension
uv run pytest                  # verify
```
A plain shell fails with `cl.exe not found`. After changing **Rust** code, rebuild:
`uv run maturin develop --release` (or plain `maturin develop` for a faster debug
build) in that same MSVC shell — Python changes need no rebuild.

**NixOS**: `nix develop` provides python3.14 + uv + rustc/cargo/maturin (and installs
git hooks); then `uv sync` and `uv run maturin develop --release`.

## Common commands

```powershell
uv run pytest                                   # all Python tests (Phase 2 + CLI + native e2e)
uv run pytest tests/test_cli.py                 # one file
uv run pytest -k dedup                          # one test / pattern
cargo test                                      # Rust tests (needs python3.dll on PATH; see README "Windows")

uv run pre-commit run --all-files                        # commit-stage: ruff lint+format, rustfmt, hygiene
uv run pre-commit run --hook-stage pre-push --all-files  # push-stage: pytest (+ clippy/cargo test if Rust changed)
cargo clippy --all-targets -- -D warnings
cargo fmt
```

### CLI (`uv run dhbw-scraper <cmd>`)

`fetch` (Phase 1) · `extract` / `extract-html` / `extract-pdf` (Phase 2) · `run`
(fetch → extract → **dedup**) · `stats` · `report` (self-contained read-only HTML
analysis via [`dashboard.py`](src/scraper/dashboard.py); includes an interactive
per-site crawl-discovery tree drawn by a vendored, inlined d3) · `delta --since <ts>`
(re-index delta for downstream) · `dedup` · `reclassify` (re-tag the
Standort/Studienabteilung/Studiengang columns after a taxonomy or `CLASSIFY_VERSION`
change; idempotent, never touches `updated_at`) · `reset-site --site NAME` (the **only**
destructive command; wipes a site's queue/crawl_log/documents/links, keeps the raw
cache). See [`cli.py`](src/scraper/cli.py) and README "Usage".

**`config.toml` is the sole source of tuning values — no CLI flag overrides it.** The
only flags are operational: global `--config PATH`; `--site NAME` on
`fetch`/`run`/`reset-site`; `--since` on `delta`; `-o`/`--open` on `report`. Adding a
`--max-pages`-style override flag is a regression, not a feature — to vary a run, edit
`config.toml` or keep a second file and pass `--config` (it is global, so it goes
*before* the subcommand). Note `crawl.max_pages` is a **per-site** budget, not a global
cap.

## Conventions

- **Conventional Commits** are enforced (commit-msg hook + CI). Types: `build`, `chore`,
  `ci`, `docs`, `feat`, `fix`, `perf`, `refactor`, `revert`, `style`, `test`. See
  [`CONTRIBUTING.md`](./CONTRIBUTING.md).
- CI (Linux) gates merges: ruff lint+format, `rustfmt --check`, clippy `-D warnings`,
  pytest, cargo test, and PR commit-subject validation.
- `robots.txt` is **deliberately not consulted** (`crawl.respect_robots` is inert,
  kept for auditability). The scraper identifies via a configurable `user_agent`
  instead — crawl politely via `request_delay_seconds` / `workers_per_host`.
- All tuning lives in [`config.toml`](./config.toml) (`[[sites]]`, `[crawl]`,
  `[extract]`, `[dedup]`, `[storage]`) and **only** there; `load_config` range-checks
  every numeric key, so bad values fail at load naming the key rather than being floored
  by the engine.
- `data/` DB and raw cache are gitignored (only `data/.gitkeep` is tracked).

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
