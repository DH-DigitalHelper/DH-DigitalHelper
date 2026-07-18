# DHBW Scraper — Correctness & Fidelity Fixes: Design

**Status:** approved · **Date:** 2026-07-16 · **Branch:** `feat/create-dh-scraper`

## Why

A review raised 24 defects. Each was independently validated against the current tree (file-by-file read plus an adversarial investigator/skeptic verification pass); **all 24 are real**, none refuted. This spec pins the *invariants* and the *semantics decisions* the fixes depend on, so the per-task TDD work has a single reference. The task breakdown lives in the implementation plan.

## Scope boundary (verified architecture)

- **Phase 1 (crawl/fetch/links/sitemap/writer) is Rust** — `rust/src/*.rs`, reached from Python only via `crawl.py → _native`. It owns *every* Phase-1 DB write.
- **Phase 2 (extract/quality/dashboard/dedup/delta/documents-storage) is Python** — `src/dhbw_scraper/*.py`.
- **Dead**: Python `fetch.py::fetch/classify` and the queue-write/claim half of `storage.py`. Superseded by Rust; unreferenced from `src/`. `fetch.ext_for` and `RawCache.path_for/has` remain **live** (used by `extract.py`).

## Invariants (the contract these fixes establish)

1. **Raw hand-off is atomic with the digest advance.** A `queue` row with `present=1 AND content_sha256 NOT NULL` **must** have a matching `raw_docs` row. Corollary: a raw-cache write failure must never advance `content_sha256` — it must surface as an error, never as a successful `changed`/`new` fetch.
2. **Corpus removals are soft.** Documents leave the corpus via a `present=0` tombstone with a bumped `updated_at`, never a hard `DELETE` — otherwise `delta()` (which derives deletions from `present=0 AND updated_at > since`) can never report them and downstream indexes keep orphans.
3. **Validators are never destroyed by a fetch that learned nothing.** An unchanged response that omits `ETag`/`Last-Modified` must not overwrite stored validators with NULL.
4. **Content is attributed only to an in-domain final URL.** Bytes are stored against a URL only if the *post-redirect* `final_url` is in-domain. The allowlist is enforced at fetch-completion, not only at enqueue/discovery.
5. **Rust and Python `init_db` are schema-equal and migration-safe.** Both must open a DB created by the other, and both must survive a DB created before a column existed. `CREATE TABLE IF NOT EXISTS` is a **no-op on an existing table**, so every added column needs an explicit `ALTER` in *both* migrate paths.

## Pinned semantics decisions

These were left open by the review and are decided here so implementations don't diverge.

### D1 · Off-domain redirect (#3) → `Skipped`, not `Error`, not `Removed`
When `final_url` is off-domain, emit `UrlMark::Checked{ content_sha256: <unchanged>, changed: false, present: true }` + `Outcome::Skipped`, `raw_doc: None`, no edges/followable, and `crawl_log.error = "redirected off-domain to <final_url>"`. Mirrors the existing `kind == "other"` skip branch.
- **Not `Error`:** an error row would be retried on every `--full` run — reintroducing #4's pathology.
- **Not `Removed`:** `reqwest` has already followed the redirect, so 301-vs-302 is invisible at this seam; tombstoning a page because of a possibly-temporary redirect is destructive. Conservative default: the page keeps `present=1` and its prior `content_sha256`.
- **Accepted limitation:** a page that permanently moves off-domain retains stale content until `reset-site`. Documented, not silently accepted.

### D2 · Empty-body 2xx (#4) → depends on prior content
- **With prior content** (`content_sha256` NOT NULL): treat as a transient no-change — `Checked{ changed: false, present: true }`, preserve validators and the prior digest, `Outcome::Unchanged`. An empty body is far more likely a blip than a real "this page is now empty".
- **Without prior content** (`content_sha256` IS NULL): `Outcome::Skipped` with `Checked{ changed:false, present:true, content_sha256: None }` and `crawl_log.error = "empty 2xx body"`. It must **not** silently become a present row that never extracts; the crawl_log line is the signal.
- Genuine transport errors (`error.is_some()`) stay on the error path.

### D3 · Removed pages must not be able to 304 (#10)
`mark_url_removed` clears `etag`/`last_modified`. A removed page's next fetch is therefore a full GET, which lands on the 2xx path where `content_outcome`'s `!present` term yields `changed = true`, re-emitting the raw-doc hand-off and resurrecting `documents`. Cost: one full body for a page that transiently 404'd. Accepted — correctness over a single conditional GET.

### D4 · Dedup tombstones and the one-time delta flood (#5)
Retiring a dedup loser sets `present=0, updated_at=now`. Consequences accepted deliberately:
- `run_dedup`'s `before`/`after` must count **present=1** rows (a tombstone keeps `COUNT(*)` flat); the physical-delete `VACUUM` guard becomes meaningless and is repurposed/removed.
- The **first** post-fix `dedup` run tombstones the whole historical duplicate backlog and therefore emits every past loser through `delta()` once. This is the point — downstream must drop those orphans — but it is a one-time flood and is called out in the runbook.
- Resurrection is safe by construction: the row survives, so a re-canonical URL takes the existing-row UPDATE branch; `UNIQUE(url)` never collides.

### D5 · Fatal write errors abort the run by design (#8)
SQLite invalidates the entire transaction on `SQLITE_FULL`/`SQLITE_IOERR`/`SQLITE_CORRUPT`; per-page SAVEPOINTs cannot rescue sibling pages from those. Continuing to write on a full or failing disk is not desirable anyway. So: **transient** codes (`BUSY`/`BUSY_SNAPSHOT`) get a bounded retry past the 15s `busy_timeout`; **fatal** codes propagate and abort the run. This is documented behavior, not a bug.

### D6 · Ordered-list ordinals are content (#13)
`_LIST_MARKER` strips only `[-*+]` bullets. A leading `N.` is never stripped, because `_HEADING` runs first and turns `## 1. Semester` into a line that is indistinguishable from a list item — and "1. Semester" / "2. Fachsemester" are meaningful DHBW ordinals. Trade-off: genuine ordered-list markers survive into `text` and count as words. Accepted as more faithful.

### D7 · `links` is advisory (#2)
No Phase-2 query reads `links`; it feeds the dashboard only. The 304 gap is therefore fixed cheaply — re-derive edges from the already-on-disk blob — rather than by redesigning links as a projection of `raw_docs`. If the graph ever becomes load-bearing for retrieval, revisit.

## Test seams

- **Rust engine:** `crawl::run_with_client(config, run_id, force_full, progress, client)` with an in-memory `HttpClient`. The `MockClient` in `rust/tests/orchestration.rs` is extended once (T0b) to express custom `final_url`, conditional `304` on `If-None-Match`, rotated ETag, custom status, and empty-body 200 — every Phase-2/3 crawl test builds on it.
- **Rust storage:** `Connection::open_in_memory()` + `init_db` inside `#[cfg(test)] mod tests` (storage.rs). Preferred over integration tests when the fix seam is a single SQL statement (e.g. #7, #10).
- **Raw-cache failure (#1):** no harness change — point `raw_dir` at a regular file so `create_dir_all` fails.
- **Python Phase 2:** `pytest` with injected extractors (in-process thread path) — no network, no Docling, no PyMuPDF.
- **Tests are offline.** Live crawls are operator-run (runbook), never CI.

## Non-goals

Redesigning the link graph; robots.txt *policy* compliance (deliberately not consulted — only `Sitemap:` discovery is added); replacing the SDD/TDD workflow; touching Phase-2 extraction throughput.
