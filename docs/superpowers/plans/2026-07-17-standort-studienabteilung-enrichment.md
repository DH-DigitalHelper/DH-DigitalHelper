# Standort + Studienabteilung Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tag every `documents` row with its location (`standort_id`), faculty (`department_id`), and study program (`study_program_id`) via deterministic rules + content keywords, wired into Phase 2 extraction plus a one-time backfill for the existing corpus.

**Architecture:** A pure classifier (`classify.py`) reads a document's `(url, site, doc)` and returns slugs; reference data lives in `taxonomy.py`. `storage.py` gains three normalized dictionary tables + four nullable `documents` columns, seeds the fixed vocabularies in `init_db`, and calls the classifier from inside `_upsert_document` (so extraction stays tagged with **zero** changes to `extract.py`). A throwaway `scripts/backfill_classification.py` runs the same classifier over the current 46 k rows.

**Tech Stack:** Python 3.14, sqlite3 (WAL), pytest. Pure Python — no Rust/Phase-1 changes, no network, no LLM.

## Global Constraints

- **No Rust / Phase-1 changes.** These tables/columns are Phase-2-owned; every Python entrypoint runs `init_db`, which creates and seeds them. Copy nothing into `src/scrape-engine/`.
- **`config.toml` is tuning-only.** Taxonomy/rule data is reference data in code (`src/scraper/taxonomy.py`), never `config.toml`.
- **Deterministic, no network, no LLM.** Same input ⇒ same output.
- **Precision over coverage.** A wrong tag is worse than `unknown`/`NULL`. Keyword matching runs only when URL rules find nothing; a tie ⇒ `unknown`.
- **Null policy:** `department_id` is **always set** (`'unknown'` row when no match); `standort_id` set for all 11 configured sites (`NULL` only for an unknown site); `study_program_id` is `NULL` unless a program matches.
- **Backfill never touches `documents.updated_at`** (derived metadata — no delta spam), mirroring `run_dedup`.
- **Conventional Commits** (`feat`, `test`, `docs`, …). Run `uv run ruff format` + `uv run ruff check` before each commit.
- Doc-dict shape (from `html_extract.extract_html`): `{"title", "text", "markdown", "lang", "word_count", "metadata": {"author","date","description","sitename"}}`. `metadata` may be `None`.

---

### Task 1: `taxonomy.py` — reference data + invariants

**Files:**
- Create: `src/scraper/taxonomy.py`
- Test: `tests/test_taxonomy.py`

**Interfaces:**
- Produces:
  - `CLASSIFY_VERSION: int`
  - `DEPARTMENTS: list[tuple[str, str]]` — `(slug, display_name)`; includes `("unknown","Unbekannt")`.
  - `STANDORTE: list[tuple[str, str, str, str | None]]` — `(slug, display_name, kind, parent_slug)`; `kind ∈ {"campus","satellite","central","cas"}`.
  - `SITE_TO_STANDORT: dict[str, str]` — `documents.site` (allowed_domain) → base standort slug.
  - `SATELLITE_RULES: list[tuple[str, tuple[str, ...], str]]` — `(satellite_slug, url_substrings, parent_slug)`.
  - `DEPARTMENT_URL_RULES: list[tuple[str, str]]` — `(url_substring, department_slug)`, priority order.
  - `DEPARTMENT_KEYWORDS: dict[str, tuple[str, ...]]` — `department_slug → keywords` (word-boundary matched).
  - `STUDY_PROGRAMS: list[tuple[str, str, str, tuple[str, ...]]]` — `(slug, display_name, department_slug, url_or_title_substrings)`, priority order.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_taxonomy.py
from scraper import taxonomy as tx

DEPT_SLUGS = {slug for slug, _ in tx.DEPARTMENTS}


def test_departments_include_the_four_faculties_and_unknown():
    assert DEPT_SLUGS == {"technik", "wirtschaft", "sozialwesen", "gesundheit", "unknown"}


def test_department_slugs_are_unique():
    slugs = [slug for slug, _ in tx.DEPARTMENTS]
    assert len(slugs) == len(set(slugs))


def test_every_satellite_parent_is_a_known_campus():
    campus_slugs = {slug for slug, _, kind, _ in tx.STANDORTE if kind == "campus"}
    for slug, _display, kind, parent in tx.STANDORTE:
        if kind == "satellite":
            assert parent in campus_slugs, f"{slug} -> unknown parent {parent}"


def test_satellite_rules_reference_defined_satellites():
    standort_slugs = {slug for slug, _, _, _ in tx.STANDORTE}
    for sat_slug, patterns, parent in tx.SATELLITE_RULES:
        assert sat_slug in standort_slugs
        assert patterns  # non-empty
        assert parent in standort_slugs


def test_site_to_standort_covers_every_configured_site():
    # The 11 allowed_domain values from config.toml.
    configured = {
        "heidenheim.dhbw.de", "www.dhbw.de", "mannheim.dhbw.de", "dhbw-stuttgart.de",
        "karlsruhe.dhbw.de", "mosbach.dhbw.de", "heilbronn.dhbw.de",
        "ravensburg.dhbw.de", "dhbw-loerrach.de", "dhbw-vs.de", "cas.dhbw.de",
    }
    assert configured <= set(tx.SITE_TO_STANDORT)
    base_slugs = {slug for slug, _, _, _ in tx.STANDORTE}
    for base in tx.SITE_TO_STANDORT.values():
        assert base in base_slugs


def test_department_rules_and_programs_map_to_known_faculties():
    for _substr, dept in tx.DEPARTMENT_URL_RULES:
        assert dept in DEPT_SLUGS
    assert set(tx.DEPARTMENT_KEYWORDS) <= DEPT_SLUGS
    for _slug, _display, dept, patterns in tx.STUDY_PROGRAMS:
        assert dept in DEPT_SLUGS and dept != "unknown"
        assert patterns
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_taxonomy.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scraper.taxonomy'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/scraper/taxonomy.py
"""Reference data for classifying documents into Standort / Studienabteilung /
Studiengang. Pure data + version, no behavior (see classify.py). This is the ONE
place rules live; bump CLASSIFY_VERSION when they change so a re-run of the
backfill is meaningfully different. NOT config.toml (which is tuning-only)."""

from __future__ import annotations

CLASSIFY_VERSION = 1

# Fixed 5 faculties. 'unknown' is a real row so department_id is never NULL.
DEPARTMENTS: list[tuple[str, str]] = [
    ("technik", "Technik"),
    ("wirtschaft", "Wirtschaft"),
    ("sozialwesen", "Sozialwesen"),
    ("gesundheit", "Gesundheit"),
    ("unknown", "Unbekannt"),
]

# (slug, display_name, kind, parent_slug). kind: campus|satellite|central|cas.
STANDORTE: list[tuple[str, str, str, str | None]] = [
    ("heidenheim", "DHBW Heidenheim", "campus", None),
    ("mannheim", "DHBW Mannheim", "campus", None),
    ("stuttgart", "DHBW Stuttgart", "campus", None),
    ("karlsruhe", "DHBW Karlsruhe", "campus", None),
    ("mosbach", "DHBW Mosbach", "campus", None),
    ("heilbronn", "DHBW Heilbronn", "campus", None),
    ("ravensburg", "DHBW Ravensburg", "campus", None),
    ("loerrach", "DHBW Lörrach", "campus", None),
    ("villingen_schwenningen", "DHBW Villingen-Schwenningen", "campus", None),
    ("cas", "DHBW Center for Advanced Studies", "cas", None),
    ("dhbw", "DHBW (Zentrale)", "central", None),
    ("stuttgart-horb", "DHBW Stuttgart – Campus Horb", "satellite", "stuttgart"),
    (
        "ravensburg-friedrichshafen",
        "DHBW Ravensburg – Campus Friedrichshafen",
        "satellite",
        "ravensburg",
    ),
    (
        "mosbach-bad-mergentheim",
        "DHBW Mosbach – Campus Bad Mergentheim",
        "satellite",
        "mosbach",
    ),
]

# documents.site (the crawl's allowed_domain) -> base standort slug.
SITE_TO_STANDORT: dict[str, str] = {
    "heidenheim.dhbw.de": "heidenheim",
    "www.dhbw.de": "dhbw",
    "mannheim.dhbw.de": "mannheim",
    "dhbw-stuttgart.de": "stuttgart",
    "karlsruhe.dhbw.de": "karlsruhe",
    "mosbach.dhbw.de": "mosbach",
    "heilbronn.dhbw.de": "heilbronn",
    "ravensburg.dhbw.de": "ravensburg",
    "dhbw-loerrach.de": "loerrach",
    "dhbw-vs.de": "villingen_schwenningen",
    "cas.dhbw.de": "cas",
}

# (satellite_slug, url_substrings, parent_slug). Applied only when the doc's base
# standort == parent_slug, so "horb" never mis-tags a non-Stuttgart page.
SATELLITE_RULES: list[tuple[str, tuple[str, ...], str]] = [
    ("stuttgart-horb", ("/horb", "horb.", ".horb"), "stuttgart"),
    (
        "ravensburg-friedrichshafen",
        ("friedrichshafen", "/fn/", "fn."),
        "ravensburg",
    ),
    ("mosbach-bad-mergentheim", ("bad-mergentheim", "mergentheim"), "mosbach"),
]

# (url_substring, department_slug), priority order. Kept SPECIFIC to preserve
# precision (e.g. no bare "gesundheit", which would catch BWL-Gesundheitsmanagement).
DEPARTMENT_URL_RULES: list[tuple[str, str]] = [
    ("soziale-arbeit", "sozialwesen"),
    ("sozialwesen", "sozialwesen"),
    ("sozialpaedagogik", "sozialwesen"),
    ("gesundheitswissenschaft", "gesundheit"),
    ("angewandte-gesundheit", "gesundheit"),
    ("hebamme", "gesundheit"),
    ("physiotherapie", "gesundheit"),
    ("maschinenbau", "technik"),
    ("elektrotechnik", "technik"),
    ("mechatronik", "technik"),
    ("fakultaet-technik", "technik"),
    ("fakultaet-wirtschaft", "wirtschaft"),
    ("betriebswirtschaft", "wirtschaft"),
]

# department_slug -> word-boundary keywords, scanned over title+description+text
# ONLY when the URL rules find nothing. A tie across faculties -> 'unknown'.
DEPARTMENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "technik": ("maschinenbau", "elektrotechnik", "mechatronik"),
    "wirtschaft": ("betriebswirtschaftslehre", "bwl"),
    "sozialwesen": ("sozialwesen", "sozialpädagogik", "sozialpaedagogik"),
    "gesundheit": ("gesundheitswissenschaften", "physiotherapie", "hebammenwissenschaft"),
}

# (slug, display_name, department_slug, url_or_title_substrings), priority order.
# Seed catalog — grows over time. A detected program sets the faculty (precedence).
STUDY_PROGRAMS: list[tuple[str, str, str, tuple[str, ...]]] = [
    ("maschinenbau", "Maschinenbau", "technik", ("maschinenbau",)),
    ("elektrotechnik", "Elektrotechnik", "technik", ("elektrotechnik",)),
    ("mechatronik", "Mechatronik", "technik", ("mechatronik",)),
    ("wirtschaftsinformatik", "Wirtschaftsinformatik", "wirtschaft", ("wirtschaftsinformatik",)),
    ("bwl-industrie", "BWL – Industrie", "wirtschaft", ("bwl-industrie",)),
    ("soziale-arbeit", "Soziale Arbeit", "sozialwesen", ("soziale-arbeit",)),
    (
        "angewandte-gesundheitswissenschaften",
        "Angewandte Gesundheitswissenschaften",
        "gesundheit",
        ("angewandte-gesundheitswissenschaften", "gesundheitswissenschaften"),
    ),
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_taxonomy.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
uv run ruff format src/scraper/taxonomy.py tests/test_taxonomy.py
uv run ruff check src/scraper/taxonomy.py tests/test_taxonomy.py
git add src/scraper/taxonomy.py tests/test_taxonomy.py
git commit -m "feat(classify): add DHBW taxonomy reference data"
```

---

### Task 2: `classify.py` — Standort (base campus + satellite)

**Files:**
- Create: `src/scraper/classify.py`
- Test: `tests/test_classify_standort.py`

**Interfaces:**
- Consumes: `taxonomy.SITE_TO_STANDORT`, `taxonomy.STANDORTE`, `taxonomy.SATELLITE_RULES`.
- Produces:
  - `classify_standort(url: str, site: str) -> str | None` — base campus from `site` (host fallback), refined to a satellite slug; `None` for an unknown site.
  - `_SATELLITE_SLUGS: frozenset[str]` (module-level; reused by Task 3 for provenance).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_classify_standort.py
from scraper import classify


def test_base_campus_from_site():
    assert classify.classify_standort("https://www.mannheim.dhbw.de/x", "mannheim.dhbw.de") == "mannheim"
    assert classify.classify_standort("https://www.dhbw-stuttgart.de/", "dhbw-stuttgart.de") == "stuttgart"
    assert classify.classify_standort("https://www.dhbw.de/alumni", "www.dhbw.de") == "dhbw"
    assert classify.classify_standort("https://www.cas.dhbw.de/", "cas.dhbw.de") == "cas"


def test_base_campus_from_host_when_site_unmapped():
    # A subdomain page whose `site` was not the exact allowed_domain still resolves.
    assert classify.classify_standort("https://events.mannheim.dhbw.de/e/1", "") == "mannheim"


def test_unknown_site_yields_none():
    assert classify.classify_standort("https://x/a", "x") is None


def test_horb_satellite_only_under_stuttgart():
    assert classify.classify_standort("https://www.dhbw-stuttgart.de/horb/its/", "dhbw-stuttgart.de") == "stuttgart-horb"
    # the substring "horb" must NOT promote a non-Stuttgart page
    assert classify.classify_standort("https://www.mannheim.dhbw.de/horbach", "mannheim.dhbw.de") == "mannheim"


def test_friedrichshafen_and_bad_mergentheim_satellites():
    assert classify.classify_standort("https://www.ravensburg.dhbw.de/friedrichshafen/", "ravensburg.dhbw.de") == "ravensburg-friedrichshafen"
    assert classify.classify_standort("https://www.mosbach.dhbw.de/bad-mergentheim/", "mosbach.dhbw.de") == "mosbach-bad-mergentheim"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_classify_standort.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scraper.classify'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/scraper/classify.py
"""Pure, deterministic classification of a document into Standort /
Studienabteilung / Studiengang. No DB, no I/O, no network -- classify(url, site,
doc) is a pure function of its inputs, so it is unit-testable from fixtures and
safe to call from both the Phase-2 write path and the one-time backfill."""

from __future__ import annotations

from urllib.parse import urlsplit

from . import taxonomy

_SATELLITE_SLUGS = frozenset(
    slug for slug, _display, kind, _parent in taxonomy.STANDORTE if kind == "satellite"
)


def _base_from_host(url: str) -> str | None:
    """Longest-suffix match of the URL host against the known allowed_domains, so
    a subdomain (events.mannheim.dhbw.de) resolves to its campus even when the
    row's `site` was not the exact allowed_domain."""
    host = (urlsplit(url).hostname or "").lower()
    best: tuple[str, str] | None = None
    for domain, slug in taxonomy.SITE_TO_STANDORT.items():
        if host == domain or host.endswith("." + domain):
            if best is None or len(domain) > len(best[0]):
                best = (domain, slug)
    return best[1] if best else None


def classify_standort(url: str, site: str) -> str | None:
    base = taxonomy.SITE_TO_STANDORT.get(site) or _base_from_host(url)
    if base is None:
        return None
    hay = url.lower()
    for sat_slug, patterns, parent in taxonomy.SATELLITE_RULES:
        if parent == base and any(p in hay for p in patterns):
            return sat_slug
    return base
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_classify_standort.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
uv run ruff format src/scraper/classify.py tests/test_classify_standort.py
uv run ruff check src/scraper/classify.py tests/test_classify_standort.py
git add src/scraper/classify.py tests/test_classify_standort.py
git commit -m "feat(classify): resolve Standort (campus + satellite) from url/site"
```

---

### Task 3: `classify.py` — department, program, and the `classify()` orchestrator

**Files:**
- Modify: `src/scraper/classify.py`
- Test: `tests/test_classify.py`

**Interfaces:**
- Consumes: `classify_standort`, `_SATELLITE_SLUGS`, all `taxonomy.*` rule tables.
- Produces:
  - `@dataclass Classification` with fields: `standort: str | None`, `department: str`, `program: str | None`, `program_display: str | None`, `meta: dict`.
  - `classify_department(url, title, description, text) -> str` — `'unknown'` on no match.
  - `classify_program(url, title, description, text) -> tuple[str, str, str] | None` — `(slug, display, department)`.
  - `classify(url: str, site: str, doc: dict) -> Classification` — orchestrates with faculty precedence **program → url → keyword → unknown**.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_classify.py
from scraper import classify


def _doc(title="", description="", text=""):
    return {"title": title, "text": text, "metadata": {"description": description}}


def test_program_detected_sets_faculty_and_program():
    c = classify.classify(
        "https://www.mosbach.dhbw.de/studienangebot/maschinenbau", "mosbach.dhbw.de", _doc()
    )
    assert c.program == "maschinenbau"
    assert c.program_display == "Maschinenbau"
    assert c.department == "technik"
    assert c.meta["department"] == "program"


def test_department_from_url_rule_without_program():
    c = classify.classify(
        "https://www.dhbw-stuttgart.de/fakultaet-wirtschaft/", "dhbw-stuttgart.de", _doc()
    )
    assert c.department == "wirtschaft"
    assert c.program is None
    assert c.meta["department"] == "url"


def test_department_from_content_keyword_fallback():
    c = classify.classify(
        "https://www.dhbw.de/aktuelles/meldung", "www.dhbw.de",
        _doc(title="Neuer Studiengang", description="Bachelor Sozialwesen an der DHBW"),
    )
    assert c.department == "sozialwesen"
    assert c.meta["department"] == "keyword"


def test_faculty_agnostic_page_is_unknown():
    c = classify.classify(
        "https://www.heilbronn.dhbw.de/datenschutz/", "heilbronn.dhbw.de",
        _doc(title="Datenschutz", description="Datenschutzerklärung"),
    )
    assert c.department == "unknown"
    assert c.program is None
    assert c.meta["department"] == "default"


def test_keyword_ambiguity_resolves_to_unknown_not_a_coinflip():
    c = classify.classify(
        "https://www.dhbw.de/x", "www.dhbw.de",
        _doc(text="Das Modul verbindet Maschinenbau und Sozialwesen thematisch."),
    )
    assert c.department == "unknown"


def test_standort_provenance_site_vs_url():
    base = classify.classify("https://www.mannheim.dhbw.de/x", "mannheim.dhbw.de", _doc())
    assert base.standort == "mannheim" and base.meta["standort"] == "site"
    sat = classify.classify("https://www.dhbw-stuttgart.de/horb/x", "dhbw-stuttgart.de", _doc())
    assert sat.standort == "stuttgart-horb" and sat.meta["standort"] == "url"


def test_meta_records_version():
    from scraper import taxonomy
    c = classify.classify("https://www.dhbw.de/x", "www.dhbw.de", _doc())
    assert c.meta["version"] == taxonomy.CLASSIFY_VERSION
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_classify.py -v`
Expected: FAIL — `AttributeError: module 'scraper.classify' has no attribute 'classify'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/scraper/classify.py` (add `import re` and `from dataclasses import dataclass` at the top with the existing imports):

```python
import re
from dataclasses import dataclass


@dataclass
class Classification:
    standort: str | None
    department: str
    program: str | None
    program_display: str | None
    meta: dict


def classify_program(url, title, description, text) -> tuple[str, str, str] | None:
    """First seed-catalog entry whose substring appears in the url or title.
    Returns (slug, display_name, department_slug); None when nothing matches."""
    hay = f"{url}\n{title}".lower()
    for slug, display, dept, patterns in taxonomy.STUDY_PROGRAMS:
        if any(p in hay for p in patterns):
            return slug, display, dept
    return None


def classify_department(url, title, description, text) -> str:
    """URL rules first (high precision); else content keywords over
    title+description+text with word-boundary matching; a tie -> 'unknown'."""
    low_url = url.lower()
    for substr, dept in taxonomy.DEPARTMENT_URL_RULES:
        if substr in low_url:
            return dept
    blob = f"{title}\n{description}\n{text}".lower()
    hits = {
        dept
        for dept, keywords in taxonomy.DEPARTMENT_KEYWORDS.items()
        if any(re.search(rf"\b{re.escape(kw)}\b", blob) for kw in keywords)
    }
    return next(iter(hits)) if len(hits) == 1 else "unknown"


def _department_source(url, title, description, text) -> str:
    low_url = url.lower()
    if any(s in low_url for s, _ in taxonomy.DEPARTMENT_URL_RULES):
        return "url"
    blob = f"{title}\n{description}\n{text}".lower()
    hits = {
        dept
        for dept, keywords in taxonomy.DEPARTMENT_KEYWORDS.items()
        if any(re.search(rf"\b{re.escape(kw)}\b", blob) for kw in keywords)
    }
    return "keyword" if len(hits) == 1 else "default"


def classify(url: str, site: str, doc: dict) -> Classification:
    title = doc.get("title") or ""
    meta = doc.get("metadata") or {}
    description = (meta.get("description") if isinstance(meta, dict) else None) or ""
    text = doc.get("text") or ""

    standort = classify_standort(url, site)
    standort_src = "url" if standort in _SATELLITE_SLUGS else "site"

    prog = classify_program(url, title, description, text)
    if prog is not None:
        slug, display, dept = prog
        return Classification(
            standort, dept, slug, display,
            {"standort": standort_src, "department": "program", "program": "match",
             "version": taxonomy.CLASSIFY_VERSION},
        )

    department = classify_department(url, title, description, text)
    dept_src = _department_source(url, title, description, text)
    return Classification(
        standort, department, None, None,
        {"standort": standort_src, "department": dept_src, "program": None,
         "version": taxonomy.CLASSIFY_VERSION},
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_classify.py tests/test_classify_standort.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
uv run ruff format src/scraper/classify.py tests/test_classify.py
uv run ruff check src/scraper/classify.py tests/test_classify.py
git add src/scraper/classify.py tests/test_classify.py
git commit -m "feat(classify): department + program classification with faculty precedence"
```

---

### Task 4: `storage.py` — dictionary tables, migration, seeding

**Files:**
- Modify: `src/scraper/storage.py` (`SCHEMA` string; `_migrate`; `init_db`; add `_seed_taxonomy`)
- Test: `tests/test_storage_taxonomy.py`

**Interfaces:**
- Consumes: `taxonomy.DEPARTMENTS`, `taxonomy.STANDORTE`.
- Produces: after `init_db`, tables `standorte`, `departments`, `study_programs` exist and are seeded (5 departments; 14 standorte with satellite `parent_id` wired); `documents` has columns `standort_id`, `department_id`, `study_program_id`, `classify_meta` and indexes `idx_documents_standort`, `idx_documents_department`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_storage_taxonomy.py
from scraper import storage as st

_OLD_DOCUMENTS_DDL = """
CREATE TABLE documents (
    id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, final_url TEXT, site TEXT NOT NULL,
    source_type TEXT NOT NULL, content_sha256 TEXT NOT NULL, title TEXT,
    text TEXT NOT NULL, markdown TEXT NOT NULL, lang TEXT, word_count INTEGER NOT NULL,
    metadata TEXT, present INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1,
    first_indexed_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""


def mem():
    conn = st.connect(":memory:")
    st.init_db(conn)
    return conn


def test_init_db_seeds_the_five_departments():
    conn = mem()
    names = {r["name"] for r in conn.execute("SELECT name FROM departments")}
    assert names == {"technik", "wirtschaft", "sozialwesen", "gesundheit", "unknown"}


def test_init_db_seeds_standorte_with_satellite_parents():
    conn = mem()
    rows = {r["name"]: r for r in conn.execute("SELECT * FROM standorte")}
    assert rows["stuttgart"]["kind"] == "campus"
    horb = rows["stuttgart-horb"]
    assert horb["kind"] == "satellite"
    assert horb["parent_id"] == rows["stuttgart"]["id"]


def test_documents_gains_classification_columns():
    conn = mem()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
    assert {"standort_id", "department_id", "study_program_id", "classify_meta"} <= cols


def test_seeding_is_idempotent():
    conn = mem()
    st.init_db(conn)  # second run
    assert conn.execute("SELECT COUNT(*) c FROM departments").fetchone()["c"] == 5
    assert conn.execute("SELECT COUNT(*) c FROM standorte").fetchone()["c"] == 14


def test_migration_adds_columns_to_preexisting_documents_table():
    conn = st.connect(":memory:")
    conn.executescript(_OLD_DOCUMENTS_DDL)
    conn.commit()
    st.init_db(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
    assert {"standort_id", "department_id", "study_program_id", "classify_meta"} <= cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_storage_taxonomy.py -v`
Expected: FAIL — `sqlite3.OperationalError: no such table: departments`

- [ ] **Step 3: Write minimal implementation**

In `src/scraper/storage.py`, add `from . import taxonomy` near the top imports. Append the three tables to the `SCHEMA` string (just before its closing `"""`):

```python
CREATE TABLE IF NOT EXISTS standorte (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    kind         TEXT NOT NULL,
    parent_id    INTEGER
);

CREATE TABLE IF NOT EXISTS departments (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS study_programs (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    department_id INTEGER
);
```

Extend `_migrate` (after the existing `text_sha256` block, before the `text_sha256` index) to add the four columns and their indexes:

```python
    for col in ("standort_id", "department_id", "study_program_id"):
        if cols and col not in cols:
            conn.execute(f"ALTER TABLE documents ADD COLUMN {col} INTEGER")
    if cols and "classify_meta" not in cols:
        conn.execute("ALTER TABLE documents ADD COLUMN classify_meta TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_standort ON documents(standort_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_department ON documents(department_id)"
    )
```

Add a seeding helper and call it from `init_db`:

```python
def _seed_taxonomy(conn) -> None:
    """Seed the fixed vocabularies (idempotent). Departments are a fixed 5;
    standorte are seeded in two passes so a satellite's parent_id can reference an
    already-inserted campus. study_programs are interned on demand, not here."""
    conn.executemany(
        "INSERT OR IGNORE INTO departments (name, display_name) VALUES (?, ?)",
        taxonomy.DEPARTMENTS,
    )
    for slug, display, kind, _parent in taxonomy.STANDORTE:
        conn.execute(
            "INSERT OR IGNORE INTO standorte (name, display_name, kind) VALUES (?, ?, ?)",
            (slug, display, kind),
        )
    for slug, _display, _kind, parent in taxonomy.STANDORTE:
        if parent is not None:
            conn.execute(
                "UPDATE standorte SET parent_id = (SELECT id FROM standorte WHERE name = ?) "
                "WHERE name = ?",
                (parent, slug),
            )
```

Update `init_db`:

```python
def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    _seed_taxonomy(conn)
    conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_storage_taxonomy.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Run the full storage suite to prove no regression**

Run: `uv run pytest tests/test_storage_docs.py tests/test_storage_queue.py -v`
Expected: PASS (all existing tests still green)

- [ ] **Step 6: Commit**

```bash
uv run ruff format src/scraper/storage.py tests/test_storage_taxonomy.py
uv run ruff check src/scraper/storage.py tests/test_storage_taxonomy.py
git add src/scraper/storage.py tests/test_storage_taxonomy.py
git commit -m "feat(storage): add standorte/departments/study_programs tables + document columns"
```

---

### Task 5: `storage.py` — interners + classify-on-upsert wiring

**Files:**
- Modify: `src/scraper/storage.py` (add `_standort_id`, `_department_id`, `_program_id`, `_set_classification`; call from `_upsert_document`)
- Test: `tests/test_storage_classification.py`, and extend `tests/test_extract.py`

**Interfaces:**
- Consumes: `classify.classify` (Task 3), the tables/seeding (Task 4).
- Produces:
  - `_standort_id(conn, slug: str | None) -> int | None`
  - `_department_id(conn, slug: str) -> int | None`
  - `_program_id(conn, slug, display, dept_slug) -> int | None` — INSERT OR IGNORE then id (idempotent).
  - `_set_classification(conn, doc_id, url, site, doc) -> None` — UPDATEs the four columns by `id`, **without** touching `updated_at`.
  - `_upsert_document` calls `_set_classification` on the `"new"` and `"changed"` paths.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_storage_classification.py
import json

from scraper import storage as st

NOW = "2026-07-17T00:00:00"


def mem():
    conn = st.connect(":memory:")
    st.init_db(conn)
    return conn


def doc(text="hello world " * 20, title="T", description=""):
    return {
        "title": title, "text": text, "markdown": text, "lang": "de",
        "word_count": len(text.split()), "metadata": {"description": description},
    }


def test_program_id_intern_is_idempotent():
    conn = mem()
    a = st._program_id(conn, "maschinenbau", "Maschinenbau", "technik")
    b = st._program_id(conn, "maschinenbau", "Maschinenbau", "technik")
    assert a == b
    row = conn.execute("SELECT * FROM study_programs WHERE id=?", (a,)).fetchone()
    dept = conn.execute("SELECT name FROM departments WHERE id=?", (row["department_id"],)).fetchone()
    assert dept["name"] == "technik"


def test_upsert_document_sets_standort_and_department_ids():
    conn = mem()
    url = "https://www.dhbw-stuttgart.de/fakultaet-wirtschaft/"
    st.enqueue(conn, url, "dhbw-stuttgart.de", 0, None, NOW)
    st.mark_url_checked(conn, url, 200, None, None, "c1", True, True, NOW)
    st.upsert_document(conn, url, "dhbw-stuttgart.de", "html", "c1", doc(), NOW)
    row = conn.execute("SELECT * FROM documents WHERE url=?", (url,)).fetchone()
    standort = conn.execute("SELECT name FROM standorte WHERE id=?", (row["standort_id"],)).fetchone()
    dept = conn.execute("SELECT name FROM departments WHERE id=?", (row["department_id"],)).fetchone()
    assert standort["name"] == "stuttgart"
    assert dept["name"] == "wirtschaft"
    assert row["study_program_id"] is None
    assert json.loads(row["classify_meta"])["department"] == "url"


def test_upsert_document_faculty_agnostic_page_is_unknown_department():
    conn = mem()
    url = "https://www.heilbronn.dhbw.de/datenschutz/"
    st.enqueue(conn, url, "heilbronn.dhbw.de", 0, None, NOW)
    st.mark_url_checked(conn, url, 200, None, None, "c1", True, True, NOW)
    st.upsert_document(conn, url, "heilbronn.dhbw.de", "html", "c1", doc(title="Datenschutz"), NOW)
    row = conn.execute("SELECT * FROM documents WHERE url=?", (url,)).fetchone()
    dept = conn.execute("SELECT name FROM departments WHERE id=?", (row["department_id"],)).fetchone()
    assert dept["name"] == "unknown"


def test_upsert_document_detects_program_and_derives_faculty():
    conn = mem()
    url = "https://www.mosbach.dhbw.de/studienangebot/maschinenbau"
    st.enqueue(conn, url, "mosbach.dhbw.de", 0, None, NOW)
    st.mark_url_checked(conn, url, 200, None, None, "c1", True, True, NOW)
    st.upsert_document(conn, url, "mosbach.dhbw.de", "html", "c1", doc(), NOW)
    row = conn.execute("SELECT * FROM documents WHERE url=?", (url,)).fetchone()
    prog = conn.execute("SELECT name, department_id FROM study_programs WHERE id=?", (row["study_program_id"],)).fetchone()
    dept = conn.execute("SELECT name FROM departments WHERE id=?", (row["department_id"],)).fetchone()
    assert prog["name"] == "maschinenbau"
    assert dept["name"] == "technik"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_storage_classification.py -v`
Expected: FAIL — `AttributeError: module 'scraper.storage' has no attribute '_program_id'`

- [ ] **Step 3: Write minimal implementation**

In `src/scraper/storage.py`, add `from . import classify` to the top imports. Add the helpers (near `_doc_id`):

```python
def _standort_id(conn, slug):
    if slug is None:
        return None
    row = conn.execute("SELECT id FROM standorte WHERE name=?", (slug,)).fetchone()
    return row["id"] if row else None


def _department_id(conn, slug):
    row = conn.execute("SELECT id FROM departments WHERE name=?", (slug,)).fetchone()
    return row["id"] if row else None


def _program_id(conn, slug, display, dept_slug):
    if slug is None:
        return None
    conn.execute(
        "INSERT OR IGNORE INTO study_programs (name, display_name, department_id) "
        "VALUES (?, ?, ?)",
        (slug, display, _department_id(conn, dept_slug)),
    )
    row = conn.execute("SELECT id FROM study_programs WHERE name=?", (slug,)).fetchone()
    return row["id"]


def _set_classification(conn, doc_id, url, site, doc) -> None:
    """Classify (url, site, doc) and write the four enrichment columns onto the
    document row by id. Never touches updated_at -- this is derived metadata, and
    the backfill re-runs it over the whole corpus without spamming delta()."""
    cl = classify.classify(url, site, doc)
    conn.execute(
        "UPDATE documents SET standort_id=?, department_id=?, study_program_id=?, "
        "classify_meta=? WHERE id=?",
        (
            _standort_id(conn, cl.standort),
            _department_id(conn, cl.department),
            _program_id(conn, cl.program, cl.program_display, cl.department),
            json.dumps(cl.meta),
            doc_id,
        ),
    )
```

In `_upsert_document`, call `_set_classification` on the two paths that write content. After the INSERT (`return "new"`): replace `return "new"` with:

```python
        _set_classification(conn, _doc_id(url), url, site, doc)
        return "new"
```

In the `if existing["text_sha256"] != h:` ("changed") branch, replace `return "changed"` with:

```python
        _set_classification(conn, existing["id"], url, site, doc)
        return "changed"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_storage_classification.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Add an end-to-end extract assertion**

Append to `tests/test_extract.py` (reuses its `setup_raw`, `good_doc`, `cfg`, `NOW`):

```python
def test_extract_one_populates_classification_columns(tmp_path):
    conn, _ = setup_raw(tmp_path)  # present URL https://www.dhbw.de/a, site www.dhbw.de
    row = st.claim_pending_raw(conn)
    outcome = extract.extract_one(
        conn, row, cfg(tmp_path), {"html": good_doc, "pdf": good_doc}, NOW
    )
    assert outcome == "indexed"
    doc = conn.execute(
        "SELECT standort_id, department_id, classify_meta FROM documents "
        "WHERE url='https://www.dhbw.de/a'"
    ).fetchone()
    # www.dhbw.de -> central standort resolves; department falls back to 'unknown'.
    standort = conn.execute(
        "SELECT name FROM standorte WHERE id=?", (doc["standort_id"],)
    ).fetchone()
    dept = conn.execute(
        "SELECT name FROM departments WHERE id=?", (doc["department_id"],)
    ).fetchone()
    assert standort["name"] == "dhbw"
    assert dept["name"] == "unknown"
    assert doc["classify_meta"] is not None
```

- [ ] **Step 6: Run the extract suite to prove the seam and no regression**

Run: `uv run pytest tests/test_extract.py tests/test_storage_docs.py -v`
Expected: PASS (all — existing tests unaffected, new e2e test green)

- [ ] **Step 7: Commit**

```bash
uv run ruff format src/scraper/storage.py tests/test_storage_classification.py tests/test_extract.py
uv run ruff check src/scraper/storage.py tests/test_storage_classification.py tests/test_extract.py
git add src/scraper/storage.py tests/test_storage_classification.py tests/test_extract.py
git commit -m "feat(storage): classify documents on upsert (Standort/Studienabteilung/Studiengang)"
```

---

### Task 6: One-time backfill migration for the existing corpus

**Files:**
- Create: `scripts/backfill_classification.py`
- Test: `tests/test_backfill_classification.py`

**Interfaces:**
- Consumes: `storage.init_db`, `storage._set_classification`, `storage.write_txn`.
- Produces: `backfill_classification(conn, batch_size: int = 500) -> dict` returning `{"updated": int}`; a `__main__` that loads `config.toml` and runs it against the real DB.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backfill_classification.py
import importlib.util
from pathlib import Path

from scraper import storage as st

NOW = "2026-07-17T00:00:00"
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_classification.py"


def _load():
    spec = importlib.util.spec_from_file_location("backfill_classification", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def doc(text="hello world " * 20):
    return {"title": "T", "text": text, "markdown": text, "lang": "de",
            "word_count": len(text.split()), "metadata": {"description": ""}}


def _seed_unclassified(conn, url, site):
    st.enqueue(conn, url, site, 0, None, NOW)
    st.mark_url_checked(conn, url, 200, None, None, "c1", True, True, NOW)
    st.upsert_document(conn, url, site, "html", "c1", doc(), NOW)
    # simulate a pre-feature row: clear the enrichment columns
    conn.execute(
        "UPDATE documents SET standort_id=NULL, department_id=NULL, "
        "study_program_id=NULL, classify_meta=NULL WHERE url=?",
        (url,),
    )
    conn.commit()


def test_backfill_populates_ids_without_touching_updated_at(tmp_path):
    mod = _load()
    conn = st.connect(str(tmp_path / "db.sqlite3"))
    st.init_db(conn)
    url = "https://www.dhbw-stuttgart.de/fakultaet-wirtschaft/"
    _seed_unclassified(conn, url, "dhbw-stuttgart.de")
    before = conn.execute("SELECT updated_at FROM documents WHERE url=?", (url,)).fetchone()["updated_at"]

    result = mod.backfill_classification(conn, batch_size=10)

    assert result["updated"] == 1
    row = conn.execute("SELECT * FROM documents WHERE url=?", (url,)).fetchone()
    dept = conn.execute("SELECT name FROM departments WHERE id=?", (row["department_id"],)).fetchone()
    assert dept["name"] == "wirtschaft"
    assert row["standort_id"] is not None
    assert row["updated_at"] == before  # untouched


def test_backfill_is_idempotent(tmp_path):
    mod = _load()
    conn = st.connect(str(tmp_path / "db.sqlite3"))
    st.init_db(conn)
    _seed_unclassified(conn, "https://www.mannheim.dhbw.de/x", "mannheim.dhbw.de")
    mod.backfill_classification(conn, batch_size=10)
    first = conn.execute("SELECT standort_id, department_id, classify_meta FROM documents").fetchone()
    mod.backfill_classification(conn, batch_size=10)
    second = conn.execute("SELECT standort_id, department_id, classify_meta FROM documents").fetchone()
    assert tuple(first) == tuple(second)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_backfill_classification.py -v`
Expected: FAIL — `FileNotFoundError` / spec load error (`scripts/backfill_classification.py` missing)

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/backfill_classification.py
"""ONE-TIME backfill: classify every existing document into Standort /
Studienabteilung / Studiengang, populating the enrichment columns that the
Phase-2 write path now fills going forward.

Run once against the current corpus, then `git rm` this file -- the permanent
logic lives in scraper.classify / scraper.storage, which this only drives.

    uv run python scripts/backfill_classification.py            # uses config.toml
    uv run python scripts/backfill_classification.py --config other.toml

Keyset-paginated by documents.id (a forward index range scan, like run_dedup) so
only `batch_size` rows are resident at once. Never touches updated_at."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scraper import storage  # noqa: E402
from scraper.config import load_config  # noqa: E402


def backfill_classification(conn, batch_size: int = 500) -> dict:
    storage.init_db(conn)  # ensure tables/columns/seed exist
    updated = 0
    last_id = ""
    while True:
        rows = conn.execute(
            "SELECT id, url, site, title, text, metadata FROM documents "
            "WHERE id > ? ORDER BY id LIMIT ?",
            (last_id, batch_size),
        ).fetchall()
        if not rows:
            break
        with storage.write_txn(conn):
            for r in rows:
                doc = {
                    "title": r["title"],
                    "text": r["text"] or "",
                    "metadata": json.loads(r["metadata"]) if r["metadata"] else None,
                }
                storage._set_classification(conn, r["id"], r["url"], r["site"], doc)
        updated += len(rows)
        last_id = rows[-1]["id"]
    return {"updated": updated}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--batch-size", type=int, default=None)
    args = parser.parse_args(argv)

    config = load_config(args.config)
    batch = args.batch_size or config.dedup.batch_size
    conn = storage.connect(config.storage.db_file)
    try:
        result = backfill_classification(conn, batch_size=batch)
    finally:
        conn.close()
    print(f"backfill complete: {result['updated']} documents classified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_backfill_classification.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
uv run ruff format scripts/backfill_classification.py tests/test_backfill_classification.py
uv run ruff check scripts/backfill_classification.py tests/test_backfill_classification.py
git add scripts/backfill_classification.py tests/test_backfill_classification.py
git commit -m "feat(scripts): one-time backfill of document classification"
```

---

### Task 7: Surface coverage in `stats` (verification aid)

**Files:**
- Modify: `src/scraper/storage.py` (`stats`)
- Test: extend `tests/test_storage_classification.py`

**Interfaces:**
- Consumes: the populated `documents` + dictionary tables.
- Produces: `stats(conn)` gains `by_department: dict[str,int]`, `by_standort: dict[str,int]`, and `unclassified: int` (present docs with `standort_id IS NULL OR department_id IS NULL`). Existing keys unchanged.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_storage_classification.py
def test_stats_reports_department_and_standort_breakdown():
    conn = mem()
    url = "https://www.dhbw-stuttgart.de/fakultaet-wirtschaft/"
    st.enqueue(conn, url, "dhbw-stuttgart.de", 0, None, NOW)
    st.mark_url_checked(conn, url, 200, None, None, "c1", True, True, NOW)
    st.upsert_document(conn, url, "dhbw-stuttgart.de", "html", "c1", doc(), NOW)

    s = st.stats(conn)
    assert s["by_department"].get("wirtschaft") == 1
    assert s["by_standort"].get("stuttgart") == 1
    assert s["unclassified"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_storage_classification.py::test_stats_reports_department_and_standort_breakdown -v`
Expected: FAIL — `KeyError: 'by_department'`

- [ ] **Step 3: Write minimal implementation**

In `storage.stats`, before the `return {...}`, compute the breakdowns and add them to the returned dict:

```python
    by_department = conn.execute(
        "SELECT d.name, COUNT(*) c FROM documents doc "
        "JOIN departments d ON d.id = doc.department_id "
        "WHERE doc.present=1 GROUP BY d.name"
    ).fetchall()
    by_standort = conn.execute(
        "SELECT s.name, COUNT(*) c FROM documents doc "
        "JOIN standorte s ON s.id = doc.standort_id "
        "WHERE doc.present=1 GROUP BY s.name"
    ).fetchall()
    unclassified = scalar(
        "SELECT COUNT(*) FROM documents "
        "WHERE present=1 AND (standort_id IS NULL OR department_id IS NULL)"
    )
```

Add to the returned dict:

```python
        "by_department": {r["name"]: r["c"] for r in by_department},
        "by_standort": {r["name"]: r["c"] for r in by_standort},
        "unclassified": unclassified,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_storage_classification.py -v`
Expected: PASS (all)

- [ ] **Step 5: Full suite + commit**

```bash
uv run pytest
uv run ruff format src/scraper/storage.py tests/test_storage_classification.py
uv run ruff check src/scraper/storage.py tests/test_storage_classification.py
git add src/scraper/storage.py tests/test_storage_classification.py
git commit -m "feat(stats): report Standort/Studienabteilung coverage breakdown"
```

---

## Running the backfill (operator step — after all tasks land)

The migration is code you run once against your real 3.8 GB DB. It is idempotent
and never rewrites `updated_at`, so it is safe to re-run:

```powershell
# from the x64 Native Tools shell (any shell works — this touches no Rust)
uv run python scripts/backfill_classification.py
uv run dhbw-scraper stats          # eyeball by_department / by_standort / unclassified
```

When satisfied, retire the throwaway migration:

```bash
git rm scripts/backfill_classification.py tests/test_backfill_classification.py
git commit -m "chore: remove one-time classification backfill"
```

## Self-Review

**Spec coverage:**
- Three dictionary tables + id columns → Task 4. ✅
- Null policy (department always set, standort for known sites, program sparse) → Tasks 3, 5 + tests. ✅
- Pure classifier `classify_standort/department/program/classify` → Tasks 2, 3. ✅
- Faculty precedence (program → url → keyword → unknown) → Task 3 tests. ✅
- Content-keyword fallback with ambiguity → unknown → Task 3 tests. ✅
- Taxonomy as checked-in reference data (not config.toml) → Task 1. ✅
- Inline wiring via `_upsert_document` (no extract.py change) → Task 5. ✅
- Throwaway backfill, keyset-paginated, `updated_at` untouched, idempotent → Task 6. ✅
- `classify_meta` provenance + `CLASSIFY_VERSION` → Tasks 1, 3, 5. ✅
- Coverage visibility → Task 7. ✅

**Placeholder scan:** No TBD/TODO; every code step is complete and runnable.

**Type consistency:** `Classification(standort, department, program, program_display, meta)` is produced in Task 3 and consumed in Task 5 (`cl.standort/.department/.program/.program_display/.meta`). Helper names `_standort_id/_department_id/_program_id/_set_classification` are defined and called consistently in Tasks 5–6. `classify.classify(url, site, doc)` signature matches all call sites.
