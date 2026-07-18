# Standort + Studienabteilung enrichment — design

**Date:** 2026-07-17
**Status:** approved (brainstorming), pending implementation plan
**Scope:** Phase 2 (Python) only — no Rust / Phase 1 changes.

## Problem

The corpus (`documents`, ~46 k present rows) records each page's `site` (the
crawled campus domain) but nothing about **which physical location** or **which
faculty / study program** the page belongs to. Downstream RAG retrieval wants to
**filter and route** by location and faculty (e.g. answer a Technik question from
Technik documents at a given Standort). We add three enrichment dimensions to
every document:

- **Standort** — the location: the 9 campuses + CAS + central portal, refined to
  **satellite locations** where detectable (Horb under Stuttgart, Friedrichshafen
  under Ravensburg, Bad Mergentheim under Mosbach, …).
- **Studienabteilung** (faculty) — one of **Technik, Wirtschaft, Sozialwesen,
  Gesundheit**, or **unknown**.
- **Studiengang** (study program) — the specific program when detectable, kept
  **separate** from the faculty; `NULL` when not detected.

## Goals

- Machine-reliable, controlled vocabularies suitable for RAG filtering/routing.
- High **precision** over coverage: a wrong tag is worse than `unknown`/`NULL`.
- Fully **deterministic**, config-/data-driven, re-runnable — no LLM, no network.
- Tag the **existing** corpus without a re-crawl, and keep **future** crawls
  tagged automatically.

## Non-goals

- No Phase 1 / Rust changes. The three dictionary tables and the `documents`
  id-columns are written by Python (Phase 2), like `documents` already is.
- No exhaustive up-front enumeration of all ~200+ DHBW programs. The program
  catalog starts as a curated **seed** and grows; unmatched programs are simply
  `NULL` (never a wrong guess).
- No CLI-flag tuning knobs (honors CLAUDE.md: `config.toml` is tuning-only; the
  taxonomy is reference data in code, not tuning).

## Schema (normalized, id-based — mirrors the `urls`/`links` pattern)

Three dictionary tables, added to `storage.SCHEMA`:

```sql
CREATE TABLE IF NOT EXISTS standorte (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,   -- slug: "stuttgart", "stuttgart-horb"
    display_name TEXT NOT NULL,          -- "DHBW Stuttgart", "DHBW Stuttgart – Campus Horb"
    kind         TEXT NOT NULL,          -- 'campus' | 'satellite' | 'central' | 'cas'
    parent_id    INTEGER                 -- satellite → its campus; NULL otherwise
);

CREATE TABLE IF NOT EXISTS departments (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,   -- 'technik','wirtschaft','sozialwesen','gesundheit','unknown'
    display_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS study_programs (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,  -- slug: "wirtschaftsinformatik"
    display_name  TEXT NOT NULL,
    department_id INTEGER                -- program → its faculty; NULL if unmapped
);
```

Columns added to `documents` via the existing forward-only, O(1)
`_migrate()` ALTER pattern (all nullable so a pre-existing DB is a metadata-only
change):

| column | policy |
|---|---|
| `standort_id` INTEGER | **always set** — resolves to ≥ base campus, refined to satellite when detected |
| `department_id` INTEGER | **always set** — resolves to the `'unknown'` row when no faculty matches (keeps routing filters/counts total) |
| `study_program_id` INTEGER | **NULL** unless a specific program is detected |
| `classify_meta` TEXT | small JSON: per-field provenance (`url`/`keyword`/`default`) + `classify_version`. For auditing rule precision. |

Indexes: `idx_documents_standort`, `idx_documents_department` on `documents`, to
support routing filters. `_migrate()` adds the columns then the indexes (same
ordering constraint already handled for `text_sha256`).

### Dictionary seeding

- **departments**: the fixed 5 rows seeded in `init_db` (idempotent
  `INSERT OR IGNORE`). `'unknown'` is a real row so `department_id` is never NULL.
- **standorte**: campuses/central/CAS seeded from the taxonomy (11 base rows);
  satellite rows seeded from the taxonomy too, with `parent_id` wired to their
  campus.
- **study_programs**: interned on demand (`INSERT OR IGNORE` + id lookup) as
  documents are classified, `department_id` taken from the catalog entry.

## Classifier — `src/scraper/classify.py` (pure; no DB, no I/O)

Pure functions of a document's `(url, title, description, text)`:

- `classify_standort(url) -> str` — base campus from host/`site`, refined to a
  satellite slug by URL host/path rules; always returns a known slug.
- `classify_department(url, title, description, text) -> str` — **URL path +
  program-catalog rules first** (high precision), **content-keyword fallback** on
  title/description/text; returns `'unknown'` when nothing matches.
- `classify_program(url, title, description, text) -> str | None` — matches the
  seed program catalog; `None` when absent.
- `classify(url, doc) -> Classification` — orchestrates the three; returns slugs
  + `NULL`/`None` + a provenance dict for `classify_meta`.

**Faculty precedence (unambiguous ordering):** (1) if a **program** is detected,
`department` = that program's catalog faculty (provenance `program`); else (2)
**URL rules**; else (3) **content keywords**; else (4) `'unknown'`. So a detected
program never disagrees with the faculty it implies.

`Classification` is a small dataclass: `standort: str`, `department: str`,
`program: str | None`, `meta: dict`. No DB access ⇒ unit-testable purely from
URL/text fixtures.

**Content-keyword fallback precision guard:** keyword matching only runs when the
URL rules yield no faculty, and uses word-boundary matching against curated
per-faculty keyword sets. Ambiguity (two faculties tie) → `'unknown'`, never a
coin-flip.

## Taxonomy / rules data — `src/scraper/taxonomy.py` (checked-in reference data)

Not `config.toml` (reserved for tuning). A versioned, testable Python module:

- `CLASSIFY_VERSION` — bump when rules/data change (recorded in `classify_meta`;
  signals when a re-run of the migration is worthwhile).
- `STANDORTE` — campus/central/CAS list + satellite entries (slug, display,
  kind, parent, and host/path match rules).
- `DEPARTMENTS` — the fixed 5.
- `DEPARTMENT_URL_RULES` — path substrings/segments → faculty (high precision).
- `DEPARTMENT_KEYWORDS` — per-faculty keyword sets for the content fallback.
- `STUDY_PROGRAMS` — **seed** catalog: slug, display, faculty, and match rules
  (URL codes like `/wi/`, title patterns). Grows over time.

## Wiring

Both paths call the **same** `classify.classify()`; only the write differs.

### Inline (permanent) — Phase 2 extract

`_materialize()` already loops `urls_for_content(digest)` and calls
`_upsert_document(url, site, source_type, digest, doc, now)`. Classification is
**per-URL over the shared doc**, so it belongs in that loop:

- `_upsert_document` computes `classify.classify(url, doc)` and, when it writes a
  document row, interns standort/department/program → ids (new storage helpers
  `intern_standort/intern_department/intern_program`) and writes the four columns.
- Dictionary interning composes inside the existing `write_txn` so a doc's
  materialization stays atomic.
- Dedup interaction: `_upsert_document` already picks the canonical (cleanest)
  URL per text; classification is computed for whichever URL wins, which is
  correct (that URL is the one indexed).

### One-time backfill (throwaway) — `scripts/backfill_classification.py`

- Keyset-paginated over `present=1` documents by `id` (same pattern as
  `run_dedup`) so the large corpus streams in `batch_size` chunks; only that many
  rows resident at once.
- For each row: `classify.classify(url, {title, text, metadata})`, intern ids,
  UPDATE the four columns. **Does not touch `updated_at`** (derived metadata — no
  delta spam), matching `run_dedup`'s backfill discipline.
- Idempotent: a second run recomputes the same ids and writes identical values.
- Intended to be run once, then `git rm`'d. It imports the permanent
  `classify`/`taxonomy`/`storage` modules, so removing it leaves no dead code.

## Testing (TDD — tests first)

- `tests/test_classify.py` (pure, the precision spec):
  - base campus per site; each satellite (Horb/Friedrichshafen/Bad Mergentheim);
  - each of the 4 faculties via a URL rule and via the keyword fallback;
  - a few seed programs → correct program + derived faculty;
  - faculty-agnostic pages (datenschutz/impressum/events) → `unknown` / `None`;
  - keyword ambiguity → `unknown` (no coin-flip);
  - provenance recorded correctly in `meta`.
- `tests/test_storage_classification.py`: `init_db` seeds departments +
  standorte; interning is idempotent (same slug → same id); columns land on the
  document row.
- Extend an existing extract test to assert `_materialize` populates the ids and
  dictionary tables end-to-end.
- Migration test: run the backfill over a small seeded DB, assert ids populated
  and `updated_at` unchanged; second run is a no-op (idempotent).

## Risks / mitigations

- **Rule coverage gaps** → many `unknown`. Acceptable by design (precision over
  coverage); `classify_meta` provenance + a `stats`/dashboard breakdown make gaps
  visible so the taxonomy can grow.
- **Keyword false positives** → guarded by "URL rules first, keywords only on
  no-match, ambiguity → unknown, word-boundary matching".
- **Program catalog drift** → `CLASSIFY_VERSION` bump + re-run migration; inline
  path self-heals on the next extract.

## Resolved decisions

- `classify_meta` provenance column: **kept**.
- Program catalog: **seed then grow**.
- Faculty set: **4 + unknown**. Standort: **campuses + satellites**. Storage:
  **normalized dictionary tables with ids**. Derivation: **rules + content
  keywords**. Delivery: **inline in extract + throwaway one-time migration**.
