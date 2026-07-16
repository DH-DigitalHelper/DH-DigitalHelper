# DHBW Dual-Site Scraper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a resumable, incremental, two-phase scraper that crawls `heidenheim.dhbw.de` and `www.dhbw.de`, downloads HTML + PDFs, extracts clean content (trafilatura for HTML, Docling for PDF), quality-filters it, and stores a per-URL corpus in SQLite with change-detection deltas for scheduled re-indexing.

**Architecture:** Phase 1 (`fetch`) crawls the persistent SQLite `queue` with polite conditional GETs, discovers links, and caches raw bytes + an extraction hand-off. Phase 2 (`extract`) claims cached content, runs the heavy extractors, applies the quality gate, and upserts per-URL `documents`. Both phases are resumable (state columns), parallel-safe (atomic `BEGIN IMMEDIATE` row claims on a WAL database), and observable (stderr progress).

**Tech Stack:** Python 3.11+, stdlib `sqlite3`/`urllib`/`html.parser`/`concurrent.futures`, `trafilatura` (HTML), `docling` (PDF), `pytest`, `uv`.

## Global Constraints

- Python `>=3.11` (uses `tomllib`, `X | None` types).
- Package layout: `src/dhbw_scraper/`, console script `dhbw-scraper = "dhbw_scraper.cli:main"`.
- Two sites, **strict per-domain**: only follow links whose host equals or ends with `.<allowed_domain>`; record but never follow other hosts.
- HTML → `trafilatura`; PDF → `docling`. No `pdfplumber`.
- robots.txt is **ignored** (deliberate); every request uses the honest configurable `user_agent` and a per-host `request_delay_seconds` (default 1.0).
- SQLite in **WAL** mode; `busy_timeout=5000`; row claims use `BEGIN IMMEDIATE`.
- Document identity is the **URL** (`id = sha1(url)[:16]`). Content dedup/extraction is by **content sha256**.
- Quality gate default `min_words = 50`.
- Progress is **plain stderr** — no `rich` or other CLI deps.
- TDD throughout: failing test → minimal impl → passing test → commit. Tests run **offline** (network + Docling stubbed).
- Reference spec: `docs/superpowers/specs/2026-07-14-dhbw-dual-site-scraper-design.md`.

---

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | Deps (`trafilatura`, `docling`, dev `pytest`), console script |
| `config.toml` | Multi-site + crawl/extract/storage config |
| `src/dhbw_scraper/config.py` | Load/validate config into dataclasses |
| `src/dhbw_scraper/fetch.py` | Polite conditional-GET HTTP, content-type classification |
| `src/dhbw_scraper/links.py` | `<a href>` discovery + in-domain filtering |
| `src/dhbw_scraper/storage.py` | SQLite schema, atomic claims, dedup, upserts, delta/stats, raw cache |
| `src/dhbw_scraper/quality.py` | Moderate accept/reject gate |
| `src/dhbw_scraper/html_extract.py` | trafilatura → title/text/markdown/metadata |
| `src/dhbw_scraper/pdf_extract.py` | Docling → title/text/markdown/metadata |
| `src/dhbw_scraper/sitemap.py` | Sitemap URL + `<lastmod>` discovery |
| `src/dhbw_scraper/crawl.py` | Phase 1 orchestration (change detection, worker loop) |
| `src/dhbw_scraper/extract.py` | Phase 2 orchestration (extract, gate, materialize) |
| `src/dhbw_scraper/progress.py` | stderr progress reporter |
| `src/dhbw_scraper/cli.py` | `fetch` / `extract` / `run` / `stats` / `delta` |
| `tests/…` | One test module per source module |

---

## Task 1: Project scaffolding, dependencies, config

**Files:**
- Modify: `pyproject.toml`
- Create: `config.toml`
- Create: `src/dhbw_scraper/__init__.py`, `src/dhbw_scraper/__main__.py`
- Create: `src/dhbw_scraper/config.py`
- Test: `tests/test_config.py`, `tests/__init__.py`

**Interfaces:**
- Produces: `Site(name, seed_url, allowed_domain)`; `CrawlConfig(use_sitemap, max_pages, request_delay_seconds, respect_robots, workers_per_host, recheck, user_agent)`; `ExtractConfig(workers, min_words)`; `StorageConfig(db_file: Path, raw_dir: Path)`; `Config(root: Path, sites: list[Site], crawl, extract, storage)`; `load_config(path: Path | None) -> Config`; `find_config(start: Path | None) -> Path`.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "dhbw-scraper"
version = "0.2.0"
description = "Incremental dual-site scraper + extractor for DHBW content (RAG stage 1)"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    "trafilatura>=2.1.0",
]

[project.scripts]
dhbw-scraper = "dhbw_scraper.cli:main"

[project.optional-dependencies]
# Docling pulls torch + downloads models — heavy. It is imported lazily
# (dhbw_scraper.pdf_extract._build_converter), so the test suite never needs it
# installed. A real PDF run requires: uv sync --extra pdf
pdf = ["docling>=2.0"]
dev = ["pytest>=8.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/dhbw_scraper"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2: Write `config.toml`**

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
max_pages = 0
request_delay_seconds = 1.0
respect_robots = false
workers_per_host = 1
recheck = "all"                       # "all" | "changed-only"
# Honest, contactable UA. Fill in a real contact before a full run.
user_agent = "dhbw-scraper/0.2 (+https://github.com/deadmade/Integrationsseminar; contact: CONTACT_EMAIL)"

[extract]
workers = 4
min_words = 50

[storage]
db_file = "data/scraper.sqlite3"
raw_dir = "data/raw"
```

- [ ] **Step 3: Create empty `src/dhbw_scraper/__init__.py` and `tests/__init__.py`**

Both files are empty.

- [ ] **Step 4: Create `src/dhbw_scraper/__main__.py`**

```python
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Write the failing test `tests/test_config.py`**

```python
from pathlib import Path

from dhbw_scraper.config import load_config


def test_load_config_parses_sites_and_sections(tmp_path: Path):
    (tmp_path / "config.toml").write_text(
        """
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
max_pages = 5
request_delay_seconds = 0.5
respect_robots = false
workers_per_host = 2
recheck = "changed-only"
user_agent = "ua"

[extract]
workers = 3
min_words = 40

[storage]
db_file = "data/db.sqlite3"
raw_dir = "data/raw"
""",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path / "config.toml")

    assert cfg.root == tmp_path
    assert [s.name for s in cfg.sites] == ["heidenheim", "dhbw"]
    assert cfg.sites[1].allowed_domain == "www.dhbw.de"
    assert cfg.crawl.max_pages == 5
    assert cfg.crawl.workers_per_host == 2
    assert cfg.crawl.recheck == "changed-only"
    assert cfg.extract.min_words == 40
    assert cfg.storage.db_file == (tmp_path / "data/db.sqlite3").resolve()
    assert cfg.storage.raw_dir == (tmp_path / "data/raw").resolve()
```

- [ ] **Step 6: Run the test, verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL (`ModuleNotFoundError: dhbw_scraper.config`).

- [ ] **Step 7: Write `src/dhbw_scraper/config.py`**

```python
"""Load and validate config.toml into typed dataclasses.

Paths resolve relative to the directory containing config.toml, so the tool
works from any working directory.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Site:
    name: str
    seed_url: str
    allowed_domain: str


@dataclass(frozen=True)
class CrawlConfig:
    use_sitemap: bool
    max_pages: int
    request_delay_seconds: float
    respect_robots: bool
    workers_per_host: int
    recheck: str
    user_agent: str


@dataclass(frozen=True)
class ExtractConfig:
    workers: int
    min_words: int


@dataclass(frozen=True)
class StorageConfig:
    db_file: Path
    raw_dir: Path


@dataclass(frozen=True)
class Config:
    root: Path
    sites: list[Site]
    crawl: CrawlConfig
    extract: ExtractConfig
    storage: StorageConfig


def find_config(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        cfg = candidate / "config.toml"
        if cfg.is_file():
            return cfg
    raise FileNotFoundError("config.toml not found in current directory or any parent.")


def load_config(path: Path | None = None) -> Config:
    cfg_path = (path or find_config()).resolve()
    root = cfg_path.parent
    with cfg_path.open("rb") as fh:
        data = tomllib.load(fh)

    def resolve(p: str) -> Path:
        return (root / p).resolve()

    sites = [
        Site(name=s["name"], seed_url=s["seed_url"], allowed_domain=s["allowed_domain"])
        for s in data["sites"]
    ]
    if not sites:
        raise ValueError("config.toml must define at least one [[sites]] entry.")

    crawl_raw = data["crawl"]
    extract_raw = data["extract"]
    storage_raw = data["storage"]

    return Config(
        root=root,
        sites=sites,
        crawl=CrawlConfig(
            use_sitemap=bool(crawl_raw.get("use_sitemap", True)),
            max_pages=int(crawl_raw.get("max_pages", 0)),
            request_delay_seconds=float(crawl_raw.get("request_delay_seconds", 1.0)),
            respect_robots=bool(crawl_raw.get("respect_robots", False)),
            workers_per_host=int(crawl_raw.get("workers_per_host", 1)),
            recheck=str(crawl_raw.get("recheck", "all")),
            user_agent=crawl_raw["user_agent"],
        ),
        extract=ExtractConfig(
            workers=int(extract_raw.get("workers", 4)),
            min_words=int(extract_raw.get("min_words", 50)),
        ),
        storage=StorageConfig(
            db_file=resolve(storage_raw["db_file"]),
            raw_dir=resolve(storage_raw["raw_dir"]),
        ),
    )
```

- [ ] **Step 8: Run the test, verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml config.toml src/dhbw_scraper/__init__.py src/dhbw_scraper/__main__.py src/dhbw_scraper/config.py tests/__init__.py tests/test_config.py
git commit -m "feat: project scaffolding, deps, and multi-site config loader"
```

---

## Task 2: SQLite schema, connection, and queue operations

**Files:**
- Create: `src/dhbw_scraper/storage.py`
- Test: `tests/test_storage_queue.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `sha256_bytes(data: bytes) -> str`
  - `connect(db_file: Path) -> sqlite3.Connection` (WAL, `row_factory=sqlite3.Row`, `busy_timeout=5000`)
  - `init_db(conn) -> None`
  - `enqueue(conn, url: str, site: str, depth: int, discovered_from: str | None, now: str) -> bool` (INSERT OR IGNORE; True if newly inserted)
  - `set_sitemap_lastmod(conn, url: str, site: str, lastmod: str | None, now: str) -> None` (upsert; marks pending if new or lastmod advanced)
  - `reset_in_progress(conn) -> int`
  - `claim_pending_url(conn, site: str) -> sqlite3.Row | None` (atomic, sets `work_state='in_progress'`)
  - `get_url_state(conn, url: str) -> sqlite3.Row | None`
  - `count_pending(conn, site: str | None = None) -> int`

- [ ] **Step 1: Write the failing test `tests/test_storage_queue.py`**

```python
from dhbw_scraper import storage as st

NOW = "2026-07-14T00:00:00"


def mem():
    conn = st.connect(":memory:")
    st.init_db(conn)
    return conn


def test_enqueue_dedupes_by_url():
    conn = mem()
    assert st.enqueue(conn, "https://x/a", "x", 0, None, NOW) is True
    assert st.enqueue(conn, "https://x/a", "x", 1, "https://x/b", NOW) is False
    assert st.count_pending(conn) == 1


def test_claim_pending_is_atomic_and_marks_in_progress():
    conn = mem()
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW)
    row = st.claim_pending_url(conn, "x")
    assert row["url"] == "https://x/a"
    # No longer claimable.
    assert st.claim_pending_url(conn, "x") is None
    assert st.count_pending(conn) == 0


def test_reset_in_progress_requeues():
    conn = mem()
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW)
    st.claim_pending_url(conn, "x")
    assert st.reset_in_progress(conn) == 1
    assert st.count_pending(conn) == 1


def test_set_sitemap_lastmod_requeues_on_advance():
    conn = mem()
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW)
    st.claim_pending_url(conn, "x")  # now in_progress
    # Same lastmod -> stays put (still not pending).
    st.set_sitemap_lastmod(conn, "https://x/a", "x", "2026-01-01", NOW)
    assert st.count_pending(conn) == 0
    # Advanced lastmod -> back to pending.
    st.set_sitemap_lastmod(conn, "https://x/a", "x", "2026-02-01", NOW)
    assert st.count_pending(conn) == 1
    # Brand-new url via sitemap -> inserted pending.
    st.set_sitemap_lastmod(conn, "https://x/b", "x", "2026-02-01", NOW)
    assert st.count_pending(conn) == 2
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/test_storage_queue.py -v`
Expected: FAIL (`ModuleNotFoundError` / attributes missing).

- [ ] **Step 3: Write `src/dhbw_scraper/storage.py` (schema + queue ops)**

```python
"""SQLite persistence: schema, atomic claims, dedup, upserts, delta, raw cache.

All functions take an explicit connection so each worker (thread/process) uses
its own. The database runs in WAL mode; writers serialise via BEGIN IMMEDIATE.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    url             TEXT PRIMARY KEY,
    site            TEXT NOT NULL,
    depth           INTEGER NOT NULL DEFAULT 0,
    discovered_from TEXT,
    work_state      TEXT NOT NULL DEFAULT 'pending',
    etag            TEXT,
    last_modified   TEXT,
    sitemap_lastmod TEXT,
    content_sha256  TEXT,
    http_status     INTEGER,
    present         INTEGER NOT NULL DEFAULT 1,
    first_seen_at   TEXT NOT NULL,
    last_checked_at TEXT,
    last_changed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_queue_state ON queue(work_state);
CREATE INDEX IF NOT EXISTS idx_queue_present ON queue(present);
CREATE INDEX IF NOT EXISTS idx_queue_content ON queue(content_sha256);

CREATE TABLE IF NOT EXISTS crawl_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    url          TEXT NOT NULL,
    final_url    TEXT,
    site         TEXT,
    status       INTEGER,
    content_type TEXT,
    sha256       TEXT,
    bytes        INTEGER,
    kind         TEXT,
    outcome      TEXT,
    error        TEXT,
    fetched_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_crawl_log_run ON crawl_log(run_id);

CREATE TABLE IF NOT EXISTS raw_docs (
    content_sha256 TEXT PRIMARY KEY,
    source_type    TEXT NOT NULL,
    raw_path       TEXT NOT NULL,
    bytes          INTEGER NOT NULL,
    first_seen_at  TEXT NOT NULL,
    extract_state  TEXT NOT NULL DEFAULT 'pending',
    title          TEXT,
    text           TEXT,
    markdown       TEXT,
    lang           TEXT,
    word_count     INTEGER,
    metadata       TEXT,
    quality_ok     INTEGER,
    reject_reason  TEXT,
    extract_error  TEXT,
    extracted_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_raw_extract_state ON raw_docs(extract_state);

CREATE TABLE IF NOT EXISTS documents (
    id               TEXT PRIMARY KEY,
    url              TEXT NOT NULL UNIQUE,
    final_url        TEXT,
    site             TEXT NOT NULL,
    source_type      TEXT NOT NULL,
    content_sha256   TEXT NOT NULL,
    title            TEXT,
    text             TEXT NOT NULL,
    markdown         TEXT NOT NULL,
    lang             TEXT,
    word_count       INTEGER NOT NULL,
    metadata         TEXT,
    present          INTEGER NOT NULL DEFAULT 1,
    revision         INTEGER NOT NULL DEFAULT 1,
    first_indexed_at TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_site ON documents(site);
CREATE INDEX IF NOT EXISTS idx_documents_updated ON documents(updated_at);
CREATE INDEX IF NOT EXISTS idx_documents_present ON documents(present);
"""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def connect(db_file) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_file), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def enqueue(conn, url, site, depth, discovered_from, now) -> bool:
    cur = conn.execute(
        """INSERT OR IGNORE INTO queue (url, site, depth, discovered_from, first_seen_at)
           VALUES (?, ?, ?, ?, ?)""",
        (url, site, depth, discovered_from, now),
    )
    conn.commit()
    return cur.rowcount > 0


def set_sitemap_lastmod(conn, url, site, lastmod, now) -> None:
    row = conn.execute(
        "SELECT sitemap_lastmod FROM queue WHERE url = ?", (url,)
    ).fetchone()
    if row is None:
        conn.execute(
            """INSERT INTO queue (url, site, sitemap_lastmod, first_seen_at)
               VALUES (?, ?, ?, ?)""",
            (url, site, lastmod, now),
        )
    else:
        advanced = lastmod is not None and (
            row["sitemap_lastmod"] is None or lastmod > row["sitemap_lastmod"]
        )
        if advanced:
            conn.execute(
                "UPDATE queue SET sitemap_lastmod = ?, work_state = 'pending' WHERE url = ?",
                (lastmod, url),
            )
    conn.commit()


def reset_in_progress(conn) -> int:
    cur = conn.execute(
        "UPDATE queue SET work_state = 'pending' WHERE work_state = 'in_progress'"
    )
    conn.commit()
    return cur.rowcount


def claim_pending_url(conn, site) -> sqlite3.Row | None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM queue WHERE site = ? AND work_state = 'pending' "
            "ORDER BY depth, url LIMIT 1",
            (site,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE queue SET work_state = 'in_progress' WHERE url = ?", (row["url"],)
        )
        conn.execute("COMMIT")
        return row
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_url_state(conn, url) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM queue WHERE url = ?", (url,)).fetchone()


def count_pending(conn, site=None) -> int:
    if site is None:
        row = conn.execute(
            "SELECT COUNT(*) c FROM queue WHERE work_state = 'pending'"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) c FROM queue WHERE work_state = 'pending' AND site = ?",
            (site,),
        ).fetchone()
    return row["c"]
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `uv run pytest tests/test_storage_queue.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/dhbw_scraper/storage.py tests/test_storage_queue.py
git commit -m "feat: SQLite schema, connection, and atomic queue operations"
```

---

## Task 3: Storage — fetch results, raw cache, extraction hand-off, documents, delta

**Files:**
- Modify: `src/dhbw_scraper/storage.py`
- Test: `tests/test_storage_docs.py`

**Interfaces:**
- Consumes: Task 2 storage functions.
- Produces:
  - `RawCache(root: Path)` with `.path_for(digest, ext) -> Path`, `.has(digest, ext) -> bool`, `.write(data, ext) -> tuple[str, Path]`
  - `record_fetch(conn, run_id, url, final_url, site, status, content_type, sha256, nbytes, kind, outcome, error, now) -> None`
  - `mark_url_checked(conn, url, http_status, etag, last_modified, content_sha256, changed: bool, present: bool, now) -> None`
  - `mark_url_error(conn, url, http_status, now) -> None`
  - `mark_url_removed(conn, url, now) -> None`
  - `upsert_raw_doc(conn, content_sha256, source_type, raw_path, nbytes, now) -> bool` (True if new/re-queued for extraction)
  - `claim_pending_raw(conn) -> sqlite3.Row | None`
  - `save_extraction(conn, content_sha256, doc: dict | None, quality_ok: bool, reject_reason: str | None, extract_error: str | None, now) -> None`
  - `urls_for_content(conn, content_sha256) -> list[sqlite3.Row]` (present rows with that current hash)
  - `upsert_document(conn, url, site, source_type, content_sha256, doc: dict, now) -> str` (`"new"|"changed"|"unchanged"`)
  - `mark_document_removed(conn, url, now) -> None`
  - `delta(conn, since: str) -> dict` (`{"upserts": [...], "deletions": [...]}`)
  - `stats(conn) -> dict`

- [ ] **Step 1: Write the failing test `tests/test_storage_docs.py`**

```python
import json

from dhbw_scraper import storage as st

NOW1 = "2026-07-14T00:00:00"
NOW2 = "2026-07-15T00:00:00"


def mem():
    conn = st.connect(":memory:")
    st.init_db(conn)
    return conn


def doc(text="hello world " * 20, md=None):
    return {
        "title": "T",
        "text": text,
        "markdown": md or text,
        "lang": "en",
        "word_count": len(text.split()),
        "metadata": {"k": "v"},
    }


def test_raw_cache_roundtrip(tmp_path):
    cache = st.RawCache(tmp_path)
    digest, path = cache.write(b"abc", ".html")
    assert cache.has(digest, ".html")
    assert path.read_bytes() == b"abc"


def test_upsert_raw_doc_new_then_idempotent():
    conn = mem()
    assert st.upsert_raw_doc(conn, "h1", "html", "/raw/h1.html", 3, NOW1) is True
    assert st.claim_pending_raw(conn)["content_sha256"] == "h1"
    # already claimed -> not pending
    assert st.claim_pending_raw(conn) is None


def test_document_upsert_lifecycle():
    conn = mem()
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW1)
    st.mark_url_checked(conn, "https://x/a", 200, None, None, "c1", True, True, NOW1)
    # first extraction -> new
    assert st.upsert_document(conn, "https://x/a", "x", "html", "c1", doc(), NOW1) == "new"
    # same content -> unchanged
    assert st.upsert_document(conn, "https://x/a", "x", "html", "c1", doc(), NOW1) == "unchanged"
    # new content -> changed, revision bumps, updated_at advances
    st.mark_url_checked(conn, "https://x/a", 200, None, None, "c2", True, True, NOW2)
    assert st.upsert_document(conn, "https://x/a", "x", "html", "c2", doc("new text " * 30), NOW2) == "changed"
    row = conn.execute("SELECT * FROM documents WHERE url='https://x/a'").fetchone()
    assert row["revision"] == 2 and row["updated_at"] == NOW2 and row["present"] == 1


def test_delta_returns_upserts_and_deletions():
    conn = mem()
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW1)
    st.upsert_document(conn, "https://x/a", "x", "html", "c1", doc(), NOW1)
    st.enqueue(conn, "https://x/b", "x", 0, None, NOW2)
    st.upsert_document(conn, "https://x/b", "x", "html", "c9", doc(), NOW2)
    st.mark_document_removed(conn, "https://x/a", NOW2)

    d = st.delta(conn, since=NOW1)
    up_urls = {u["url"] for u in d["upserts"]}
    del_urls = {u["url"] for u in d["deletions"]}
    assert "https://x/b" in up_urls
    assert "https://x/a" in del_urls


def test_urls_for_content_only_present():
    conn = mem()
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW1)
    st.mark_url_checked(conn, "https://x/a", 200, None, None, "c1", True, True, NOW1)
    st.enqueue(conn, "https://x/b", "x", 0, None, NOW1)
    st.mark_url_checked(conn, "https://x/b", 200, None, None, "c1", True, True, NOW1)
    st.mark_url_removed(conn, "https://x/b", NOW2)
    rows = st.urls_for_content(conn, "c1")
    assert [r["url"] for r in rows] == ["https://x/a"]
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/test_storage_docs.py -v`
Expected: FAIL (missing attributes).

- [ ] **Step 3: Append implementations to `src/dhbw_scraper/storage.py`**

```python
import hashlib as _hashlib
import json as _json
from pathlib import Path as _Path


class RawCache:
    """Content-addressed store for downloaded bytes under ``root``."""

    def __init__(self, root) -> None:
        self.root = _Path(root)

    def path_for(self, digest: str, ext: str) -> _Path:
        if ext and not ext.startswith("."):
            ext = "." + ext
        return self.root / f"{digest}{ext}"

    def has(self, digest: str, ext: str) -> bool:
        return self.path_for(digest, ext).is_file()

    def write(self, data: bytes, ext: str):
        digest = sha256_bytes(data)
        path = self.path_for(digest, ext)
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return digest, path


def record_fetch(conn, run_id, url, final_url, site, status, content_type,
                 sha256, nbytes, kind, outcome, error, now) -> None:
    conn.execute(
        """INSERT INTO crawl_log
           (run_id, url, final_url, site, status, content_type, sha256, bytes,
            kind, outcome, error, fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, url, final_url, site, status, content_type, sha256, nbytes,
         kind, outcome, error, now),
    )
    conn.commit()


def mark_url_checked(conn, url, http_status, etag, last_modified,
                     content_sha256, changed, present, now) -> None:
    if changed:
        conn.execute(
            """UPDATE queue SET work_state='done', http_status=?, etag=?,
                   last_modified=?, content_sha256=?, present=?,
                   last_checked_at=?, last_changed_at=? WHERE url=?""",
            (http_status, etag, last_modified, content_sha256,
             1 if present else 0, now, now, url),
        )
    else:
        conn.execute(
            """UPDATE queue SET work_state='done', http_status=?, etag=?,
                   last_modified=?, present=?, last_checked_at=? WHERE url=?""",
            (http_status, etag, last_modified, 1 if present else 0, now, url),
        )
    conn.commit()


def mark_url_error(conn, url, http_status, now) -> None:
    conn.execute(
        "UPDATE queue SET work_state='error', http_status=?, last_checked_at=? WHERE url=?",
        (http_status, now, url),
    )
    conn.commit()


def mark_url_removed(conn, url, now) -> None:
    conn.execute(
        """UPDATE queue SET work_state='done', present=0, http_status=404,
               last_checked_at=?, last_changed_at=? WHERE url=?""",
        (now, now, url),
    )
    conn.commit()


def upsert_raw_doc(conn, content_sha256, source_type, raw_path, nbytes, now) -> bool:
    existing = conn.execute(
        "SELECT extract_state FROM raw_docs WHERE content_sha256=?", (content_sha256,)
    ).fetchone()
    if existing is None:
        conn.execute(
            """INSERT INTO raw_docs (content_sha256, source_type, raw_path, bytes,
                   first_seen_at, extract_state) VALUES (?,?,?,?,?, 'pending')""",
            (content_sha256, source_type, raw_path, nbytes, now),
        )
        conn.commit()
        return True
    conn.commit()
    return False


def claim_pending_raw(conn) -> sqlite3.Row | None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM raw_docs WHERE extract_state='pending' LIMIT 1"
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE raw_docs SET extract_state='in_progress' WHERE content_sha256=?",
            (row["content_sha256"],),
        )
        conn.execute("COMMIT")
        return row
    except Exception:
        conn.execute("ROLLBACK")
        raise


def save_extraction(conn, content_sha256, doc, quality_ok, reject_reason,
                    extract_error, now) -> None:
    if extract_error is not None:
        state = "error"
    elif not quality_ok:
        state = "rejected"
    else:
        state = "done"
    d = doc or {}
    conn.execute(
        """UPDATE raw_docs SET extract_state=?, title=?, text=?, markdown=?, lang=?,
               word_count=?, metadata=?, quality_ok=?, reject_reason=?,
               extract_error=?, extracted_at=? WHERE content_sha256=?""",
        (state, d.get("title"), d.get("text"), d.get("markdown"), d.get("lang"),
         d.get("word_count"), _json.dumps(d.get("metadata")) if d.get("metadata") else None,
         1 if quality_ok else 0, reject_reason, extract_error, now, content_sha256),
    )
    conn.commit()


def urls_for_content(conn, content_sha256):
    return conn.execute(
        "SELECT * FROM queue WHERE content_sha256=? AND present=1", (content_sha256,)
    ).fetchall()


def _doc_id(url: str) -> str:
    return _hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def upsert_document(conn, url, site, source_type, content_sha256, doc, now) -> str:
    existing = conn.execute("SELECT * FROM documents WHERE url=?", (url,)).fetchone()
    meta = _json.dumps(doc.get("metadata")) if doc.get("metadata") else None
    if existing is None:
        conn.execute(
            """INSERT INTO documents (id, url, final_url, site, source_type,
                   content_sha256, title, text, markdown, lang, word_count, metadata,
                   present, revision, first_indexed_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,1,?,?)""",
            (_doc_id(url), url, url, site, source_type, content_sha256,
             doc.get("title"), doc["text"], doc["markdown"], doc.get("lang"),
             doc["word_count"], meta, now, now),
        )
        conn.commit()
        return "new"
    if existing["content_sha256"] != content_sha256:
        conn.execute(
            """UPDATE documents SET content_sha256=?, source_type=?, title=?, text=?,
                   markdown=?, lang=?, word_count=?, metadata=?, present=1,
                   revision=revision+1, updated_at=? WHERE url=?""",
            (content_sha256, source_type, doc.get("title"), doc["text"], doc["markdown"],
             doc.get("lang"), doc["word_count"], meta, now, url),
        )
        conn.commit()
        return "changed"
    conn.execute("UPDATE documents SET present=1 WHERE url=?", (url,))
    conn.commit()
    return "unchanged"


def mark_document_removed(conn, url, now) -> None:
    conn.execute(
        "UPDATE documents SET present=0, updated_at=? WHERE url=?", (now, url)
    )
    conn.commit()


def delta(conn, since):
    upserts = conn.execute(
        "SELECT * FROM documents WHERE updated_at > ? AND present=1 ORDER BY updated_at",
        (since,),
    ).fetchall()
    deletions = conn.execute(
        "SELECT id, url FROM documents WHERE present=0 AND updated_at > ?",
        (since,),
    ).fetchall()
    return {"upserts": [dict(r) for r in upserts], "deletions": [dict(r) for r in deletions]}


def stats(conn) -> dict:
    def scalar(q, *a):
        return conn.execute(q, a).fetchone()[0]

    by_reason = conn.execute(
        "SELECT reject_reason, COUNT(*) c FROM raw_docs "
        "WHERE extract_state='rejected' GROUP BY reject_reason"
    ).fetchall()
    return {
        "queue_pending": scalar("SELECT COUNT(*) FROM queue WHERE work_state='pending'"),
        "queue_done": scalar("SELECT COUNT(*) FROM queue WHERE work_state='done'"),
        "queue_error": scalar("SELECT COUNT(*) FROM queue WHERE work_state='error'"),
        "urls_present": scalar("SELECT COUNT(*) FROM queue WHERE present=1"),
        "urls_removed": scalar("SELECT COUNT(*) FROM queue WHERE present=0"),
        "raw_pending": scalar("SELECT COUNT(*) FROM raw_docs WHERE extract_state='pending'"),
        "documents": scalar("SELECT COUNT(*) FROM documents WHERE present=1"),
        "documents_removed": scalar("SELECT COUNT(*) FROM documents WHERE present=0"),
        "rejects": {r["reject_reason"]: r["c"] for r in by_reason},
    }
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `uv run pytest tests/test_storage_docs.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/dhbw_scraper/storage.py tests/test_storage_docs.py
git commit -m "feat: storage for raw cache, extraction hand-off, per-URL documents, delta"
```

---

## Task 4: Polite conditional-GET fetch + classification

**Files:**
- Create: `src/dhbw_scraper/fetch.py`
- Test: `tests/test_fetch.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `FetchResult(url, final_url, status, content_type, data, etag, last_modified, error)` with `.ok` and `.not_modified` properties.
  - `fetch(url, user_agent, etag=None, last_modified=None, timeout=30, opener=urllib.request.urlopen) -> FetchResult` — sends `If-None-Match` / `If-Modified-Since` when validators are given; a 304 returns `not_modified`. Never raises for HTTP/network errors.
  - `classify(content_type, url) -> "html" | "pdf" | "other"`
  - `ext_for(kind) -> str`

- [ ] **Step 1: Write the failing test `tests/test_fetch.py`**

```python
import io

from dhbw_scraper import fetch as f


class FakeResp:
    def __init__(self, data=b"", status=200, headers=None, url="http://x/"):
        self._data = data
        self.status = status
        self.headers = FakeHeaders(headers or {})
        self._url = url

    def read(self):
        return self._data

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeHeaders:
    def __init__(self, d):
        self._d = {k.lower(): v for k, v in d.items()}

    def get_content_type(self):
        return self._d.get("content-type", "text/html").split(";")[0]

    def get(self, k, default=None):
        return self._d.get(k.lower(), default)


def test_classify_routing():
    assert f.classify("text/html", "http://x/a") == "html"
    assert f.classify("application/pdf", "http://x/a") == "pdf"
    assert f.classify("", "http://x/a.pdf") == "pdf"
    assert f.classify("image/png", "http://x/a.png") == "other"
    assert f.classify("", "http://x/startseite") == "html"


def test_fetch_success_captures_validators():
    def opener(req, timeout=0):
        assert req.get_header("User-agent") == "ua"
        return FakeResp(b"<html>hi</html>", 200,
                        {"Content-Type": "text/html", "ETag": "W/\"abc\"",
                         "Last-Modified": "Mon, 01 Jan 2026 00:00:00 GMT"})
    r = f.fetch("http://x/a", "ua", opener=opener)
    assert r.ok and r.data == b"<html>hi</html>"
    assert r.etag == 'W/"abc"'
    assert r.last_modified == "Mon, 01 Jan 2026 00:00:00 GMT"


def test_fetch_conditional_sends_validators():
    seen = {}

    def opener(req, timeout=0):
        seen["inm"] = req.get_header("If-none-match")
        seen["ims"] = req.get_header("If-modified-since")
        return FakeResp(b"x")
    f.fetch("http://x/a", "ua", etag='"abc"', last_modified="LM", opener=opener)
    assert seen["inm"] == '"abc"'
    assert seen["ims"] == "LM"


def test_fetch_304_is_not_modified():
    import urllib.error

    def opener(req, timeout=0):
        raise urllib.error.HTTPError("http://x/a", 304, "Not Modified", {}, None)
    r = f.fetch("http://x/a", "ua", etag='"abc"', opener=opener)
    assert r.not_modified
    assert not r.ok
    assert r.status == 304


def test_fetch_404_returns_error_result():
    import urllib.error

    def opener(req, timeout=0):
        raise urllib.error.HTTPError("http://x/a", 404, "Not Found", {}, None)
    r = f.fetch("http://x/a", "ua", opener=opener)
    assert r.status == 404 and not r.ok and not r.not_modified
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/test_fetch.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Write `src/dhbw_scraper/fetch.py`**

```python
"""Polite HTTP with conditional GET and content-type routing.

Network access is isolated here so the rest of the pipeline is testable offline
(inject a fake ``opener``). Failures never raise: they come back on FetchResult.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

DEFAULT_TIMEOUT = 30


@dataclass(frozen=True)
class FetchResult:
    url: str
    final_url: str
    status: int
    content_type: str
    data: bytes
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300 and bool(self.data)

    @property
    def not_modified(self) -> bool:
        return self.status == 304


def fetch(url, user_agent, etag=None, last_modified=None,
          timeout=DEFAULT_TIMEOUT, opener=urllib.request.urlopen) -> FetchResult:
    headers = {"User-Agent": user_agent}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    req = urllib.request.Request(url, headers=headers)
    try:
        with opener(req, timeout=timeout) as resp:
            data = resp.read()
            return FetchResult(
                url=url,
                final_url=resp.geturl(),
                status=getattr(resp, "status", 200) or 200,
                content_type=resp.headers.get_content_type(),
                data=data,
                etag=resp.headers.get("ETag"),
                last_modified=resp.headers.get("Last-Modified"),
            )
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return FetchResult(url, url, 304, "", b"")
        return FetchResult(url, url, exc.code, "", b"", error=f"HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return FetchResult(url, url, 0, "", b"", error=str(exc))


_BINARY_EXT = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp", ".zip",
    ".gz", ".tar", ".rar", ".7z", ".doc", ".docx", ".xls", ".xlsx", ".ppt",
    ".pptx", ".mp4", ".mp3", ".avi", ".mov", ".wav", ".ogg", ".css", ".js",
    ".json", ".woff", ".woff2", ".ttf", ".eot",
)


def classify(content_type, url) -> str:
    ct = (content_type or "").lower()
    path = urlparse(url).path.lower()
    if "pdf" in ct or path.endswith(".pdf"):
        return "pdf"
    if "html" in ct or "xml" in ct or ct.startswith("text/"):
        return "html"
    if ct:
        return "other"
    if path.endswith(_BINARY_EXT):
        return "other"
    return "html"


def ext_for(kind) -> str:
    return {"html": ".html", "pdf": ".pdf"}.get(kind, ".bin")
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `uv run pytest tests/test_fetch.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/dhbw_scraper/fetch.py tests/test_fetch.py
git commit -m "feat: polite conditional-GET fetch with content-type classification"
```

---

## Task 5: Link discovery + in-domain filtering

**Files:**
- Create: `src/dhbw_scraper/links.py`
- Test: `tests/test_links.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `in_domain(url: str, allowed_domain: str) -> bool`
  - `discover_links(html: str, base_url: str, allowed_domain: str) -> list[str]` — absolute, defragmented, in-domain `http(s)` links; skips `mailto:`/`tel:`/`javascript:`; never raises on malformed HTML.

- [ ] **Step 1: Write the failing test `tests/test_links.py`**

```python
from dhbw_scraper.links import discover_links, in_domain


def test_in_domain_matches_host_and_subdomains():
    assert in_domain("https://www.dhbw.de/x", "www.dhbw.de")
    assert in_domain("https://sub.www.dhbw.de/x", "www.dhbw.de")
    assert not in_domain("https://mosbach.dhbw.de/x", "www.dhbw.de")


def test_discover_links_filters_and_absolutizes():
    html = """
    <a href="/studium">rel</a>
    <a href="https://www.dhbw.de/kontakt#top">abs+frag</a>
    <a href="https://other.example/x">off-domain</a>
    <a href="mailto:a@b.de">mail</a>
    <a href="doc.pdf">pdf</a>
    """
    got = discover_links(html, "https://www.dhbw.de/home", "www.dhbw.de")
    assert "https://www.dhbw.de/studium" in got
    assert "https://www.dhbw.de/kontakt" in got  # fragment stripped
    assert "https://www.dhbw.de/doc.pdf" in got
    assert all("other.example" not in u for u in got)
    assert all(not u.startswith("mailto:") for u in got)


def test_discover_links_survives_malformed_html():
    assert discover_links("<a href=", "https://www.dhbw.de/", "www.dhbw.de") == []
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/test_links.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Write `src/dhbw_scraper/links.py`**

```python
"""Cheap link discovery from HTML (stdlib only) with in-domain filtering."""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse


def in_domain(url: str, allowed_domain: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host == allowed_domain or host.endswith("." + allowed_domain)


class _LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value:
                    self.links.append(value)


def discover_links(html: str, base_url: str, allowed_domain: str) -> list[str]:
    parser = _LinkExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - malformed HTML must never abort a crawl
        return []
    out: list[str] = []
    seen: set[str] = set()
    for href in parser.links:
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        absolute, _ = urldefrag(urljoin(base_url, href))
        if not absolute.startswith(("http://", "https://")):
            continue
        if in_domain(absolute, allowed_domain) and absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `uv run pytest tests/test_links.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/dhbw_scraper/links.py tests/test_links.py
git commit -m "feat: HTML link discovery with in-domain filtering"
```

---

## Task 6: Moderate quality gate

**Files:**
- Create: `src/dhbw_scraper/quality.py`
- Test: `tests/test_quality.py`

**Interfaces:**
- Consumes: nothing (operates on the extractor doc dict shape `{title, text, markdown, ...}`).
- Produces: `evaluate(doc: dict | None, min_words: int = 50) -> tuple[bool, str]` returning `(accepted, reason)`.

- [ ] **Step 1: Write the failing test `tests/test_quality.py`**

```python
from dhbw_scraper.quality import evaluate


def test_rejects_none_and_empty():
    assert evaluate(None)[0] is False
    assert evaluate({"text": "", "markdown": ""})[0] is False


def test_rejects_too_short():
    ok, reason = evaluate({"text": "three short words", "markdown": "x"}, min_words=50)
    assert ok is False and "short" in reason


def test_rejects_nav_only_link_lists():
    md = "\n".join(f"- [Item {i}](https://x/{i})" for i in range(20))
    ok, reason = evaluate({"text": "a " * 60, "markdown": md}, min_words=50)
    assert ok is False and "boilerplate" in reason


def test_accepts_real_prose():
    text = "This is a real paragraph of useful content. " * 10
    ok, reason = evaluate({"text": text, "markdown": text}, min_words=50)
    assert ok is True and reason == "ok"
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/test_quality.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Write `src/dhbw_scraper/quality.py`**

```python
"""Moderate quality gate: decide whether an extracted doc joins the corpus."""

from __future__ import annotations

_NAV_LINK_RATIO = 0.8


def _link_line(line: str) -> bool:
    s = line.strip()
    return s.startswith(("-", "*", "+")) and "](" in s


def evaluate(doc, min_words: int = 50) -> tuple[bool, str]:
    if not doc:
        return False, "empty"
    text = (doc.get("text") or "").strip()
    if not text:
        return False, "empty"

    word_count = len(text.split())
    if word_count < min_words:
        return False, f"too short: {word_count} words"

    markdown = doc.get("markdown") or ""
    non_empty = [ln for ln in markdown.splitlines() if ln.strip()]
    if non_empty:
        link_lines = sum(1 for ln in non_empty if _link_line(ln))
        if link_lines / len(non_empty) > _NAV_LINK_RATIO:
            return False, "boilerplate/nav-only"

    return True, "ok"
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `uv run pytest tests/test_quality.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/dhbw_scraper/quality.py tests/test_quality.py
git commit -m "feat: moderate quality gate for extracted documents"
```

---

## Task 7: HTML extraction (trafilatura)

**Files:**
- Create: `src/dhbw_scraper/html_extract.py`
- Test: `tests/test_html_extract.py`, `tests/fixtures/sample.html`

**Interfaces:**
- Consumes: nothing.
- Produces: `extract_html(html: str, url: str | None = None) -> dict | None` returning `{title, text, markdown, lang, word_count, metadata}` or `None` when no main content is found.

- [ ] **Step 1: Create `tests/fixtures/sample.html`**

```html
<!DOCTYPE html>
<html lang="de">
<head><title>Bewerbung</title><meta name="description" content="Wie man sich bewirbt"></head>
<body>
<nav><ul><li><a href="/a">Nav A</a></li><li><a href="/b">Nav B</a></li></ul></nav>
<main>
<h1>Bewerbung an der DHBW</h1>
<p>Die Bewerbung erfolgt direkt bei einem Dualen Partner. Dieser Absatz enthaelt
genuegend echten Inhalt, damit die Extraktion sinnvollen Text liefert und der
Qualitaetsfilter ihn akzeptiert. Studierende bewerben sich fruehzeitig.</p>
</main>
<footer>Impressum Kontakt Datenschutz</footer>
</body>
</html>
```

- [ ] **Step 2: Write the failing test `tests/test_html_extract.py`**

```python
from pathlib import Path

from dhbw_scraper.html_extract import extract_html

FIXTURE = Path(__file__).parent / "fixtures" / "sample.html"


def test_extracts_main_content_and_strips_boilerplate():
    doc = extract_html(FIXTURE.read_text(encoding="utf-8"),
                       url="https://www.heidenheim.dhbw.de/bewerbung")
    assert doc is not None
    assert "Dualen Partner" in doc["text"]
    assert "Impressum" not in doc["text"]      # footer stripped
    assert "Nav A" not in doc["text"]          # nav stripped
    assert doc["word_count"] > 20
    assert doc["markdown"]


def test_returns_none_for_contentless_html():
    assert extract_html("<html><body></body></html>") is None
```

- [ ] **Step 3: Run the test, verify it fails**

Run: `uv run pytest tests/test_html_extract.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 4: Write `src/dhbw_scraper/html_extract.py`**

```python
"""Extract main content + metadata from HTML using trafilatura."""

from __future__ import annotations

import trafilatura
from trafilatura.metadata import extract_metadata


def extract_html(html: str, url: str | None = None) -> dict | None:
    markdown = trafilatura.extract(
        html, url=url, output_format="markdown",
        include_comments=False, include_tables=True, favor_recall=True,
    )
    if not markdown:
        return None

    text = trafilatura.extract(
        html, url=url, output_format="txt",
        include_comments=False, include_tables=True, favor_recall=True,
    ) or markdown

    meta = extract_metadata(html, default_url=url)
    return {
        "title": getattr(meta, "title", None) if meta else None,
        "text": text,
        "markdown": markdown,
        "lang": None,
        "word_count": len(text.split()),
        "metadata": {
            "author": getattr(meta, "author", None) if meta else None,
            "date": getattr(meta, "date", None) if meta else None,
            "description": getattr(meta, "description", None) if meta else None,
            "sitename": getattr(meta, "sitename", None) if meta else None,
        },
    }
```

- [ ] **Step 5: Run the tests, verify they pass**

Run: `uv run pytest tests/test_html_extract.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add src/dhbw_scraper/html_extract.py tests/test_html_extract.py tests/fixtures/sample.html
git commit -m "feat: HTML main-content extraction via trafilatura"
```

---

## Task 8: PDF extraction (Docling)

**Files:**
- Create: `src/dhbw_scraper/pdf_extract.py`
- Test: `tests/test_pdf_extract.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `extract_pdf(data: bytes, converter=None) -> dict | None` — writes bytes to a temp file, converts with a Docling `DocumentConverter`, returns `{title, text, markdown, lang, word_count, metadata}` or `None` if empty. `converter` is injectable so tests avoid loading Docling.
  - `_build_converter()` — lazily constructs a real Docling converter (imported inside the function so importing the module is cheap).

- [ ] **Step 1: Write the failing test `tests/test_pdf_extract.py`**

```python
from dhbw_scraper.pdf_extract import extract_pdf


class FakeDoc:
    def export_to_markdown(self):
        return "# Modulhandbuch\n\nInhalt des Moduls mit ausreichend Text."


class FakeResult:
    document = FakeDoc()


class FakeConverter:
    def __init__(self):
        self.calls = 0

    def convert(self, path):
        self.calls += 1
        return FakeResult()


def test_extract_pdf_uses_converter_and_shapes_doc():
    conv = FakeConverter()
    doc = extract_pdf(b"%PDF-1.4 fake", converter=conv)
    assert conv.calls == 1
    assert doc is not None
    assert "Modulhandbuch" in doc["markdown"]
    assert "Inhalt des Moduls" in doc["text"]
    assert doc["word_count"] > 0
    assert doc["title"] == "Modulhandbuch"   # title pulled from first H1


def test_extract_pdf_returns_none_when_empty():
    class EmptyDoc:
        def export_to_markdown(self):
            return "   "

    class EmptyResult:
        document = EmptyDoc()

    class EmptyConv:
        def convert(self, path):
            return EmptyResult()

    assert extract_pdf(b"%PDF fake", converter=EmptyConv()) is None
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/test_pdf_extract.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Write `src/dhbw_scraper/pdf_extract.py`**

```python
"""Extract structured text from PDF bytes using Docling.

Docling is heavy (torch + models). The converter is built lazily and can be
injected in tests so the module imports cheaply and tests stay offline.
"""

from __future__ import annotations

import tempfile
from pathlib import Path


def _build_converter():
    from docling.document_converter import DocumentConverter

    return DocumentConverter()


def extract_pdf(data: bytes, converter=None) -> dict | None:
    if converter is None:
        converter = _build_converter()

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        tmp.write(data)
        tmp.flush()
        result = converter.convert(Path(tmp.name))

    markdown = (result.document.export_to_markdown() or "").strip()
    if not markdown:
        return None

    # Docling markdown is already plain-text friendly; use it for both fields.
    text = markdown
    title = None
    for line in markdown.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break

    return {
        "title": title,
        "text": text,
        "markdown": markdown,
        "lang": None,
        "word_count": len(text.split()),
        "metadata": {"extractor": "docling"},
    }
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `uv run pytest tests/test_pdf_extract.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/dhbw_scraper/pdf_extract.py tests/test_pdf_extract.py
git commit -m "feat: PDF extraction via Docling (injectable converter)"
```

---

## Task 9: Sitemap discovery

**Files:**
- Create: `src/dhbw_scraper/sitemap.py`
- Test: `tests/test_sitemap.py`

**Interfaces:**
- Consumes: `links.in_domain`.
- Produces: `discover(seed_url: str, allowed_domain: str, fetch_fn, user_agent: str) -> list[tuple[str, str | None]]` — returns `(url, lastmod)` pairs found in the site's sitemap(s), in-domain only. `fetch_fn(url, user_agent) -> FetchResult` is injected. Best-effort: parses `<url><loc>…</loc><lastmod>…</lastmod></url>` and follows `<sitemap><loc>` indexes one level.

- [ ] **Step 1: Write the failing test `tests/test_sitemap.py`**

```python
from dhbw_scraper import sitemap
from dhbw_scraper.fetch import FetchResult

INDEX = b"""<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.dhbw.de/sitemap-1.xml</loc></sitemap>
</sitemapindex>"""

URLSET = b"""<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.dhbw.de/studium</loc><lastmod>2026-02-01</lastmod></url>
  <url><loc>https://other.example/x</loc></url>
</urlset>"""


def make_fetch(mapping):
    def _fetch(url, user_agent):
        if url in mapping:
            return FetchResult(url, url, 200, "application/xml", mapping[url])
        return FetchResult(url, url, 404, "", b"", error="HTTP 404")
    return _fetch


def test_discover_follows_index_and_filters_domain():
    fetch_fn = make_fetch({
        "https://www.dhbw.de/sitemap.xml": INDEX,
        "https://www.dhbw.de/sitemap-1.xml": URLSET,
    })
    pairs = sitemap.discover("https://www.dhbw.de", "www.dhbw.de", fetch_fn, "ua")
    urls = dict(pairs)
    assert "https://www.dhbw.de/studium" in urls
    assert urls["https://www.dhbw.de/studium"] == "2026-02-01"
    assert all("other.example" not in u for u, _ in pairs)
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/test_sitemap.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Write `src/dhbw_scraper/sitemap.py`**

```python
"""Best-effort sitemap discovery: (url, lastmod) pairs, in-domain only."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from .links import in_domain

_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_URL_BLOCK = re.compile(r"<url>(.*?)</url>", re.IGNORECASE | re.DOTALL)
_SITEMAP_BLOCK = re.compile(r"<sitemap>(.*?)</sitemap>", re.IGNORECASE | re.DOTALL)
_LASTMOD = re.compile(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", re.IGNORECASE)


def _parse(xml: str):
    """Return (url_pairs, sub_sitemaps) from one sitemap document."""
    url_pairs: list[tuple[str, str | None]] = []
    for block in _URL_BLOCK.findall(xml):
        loc = _LOC.search(block)
        if not loc:
            continue
        lm = _LASTMOD.search(block)
        url_pairs.append((loc.group(1), lm.group(1) if lm else None))
    subs = [_LOC.search(b).group(1) for b in _SITEMAP_BLOCK.findall(xml) if _LOC.search(b)]
    return url_pairs, subs


def discover(seed_url, allowed_domain, fetch_fn, user_agent):
    parsed = urlparse(seed_url)
    homepage = f"{parsed.scheme}://{parsed.netloc}"
    to_visit = [f"{homepage}/sitemap.xml"]
    visited: set[str] = set()
    found: dict[str, str | None] = {}

    while to_visit:
        target = to_visit.pop()
        if target in visited:
            continue
        visited.add(target)
        result = fetch_fn(target, user_agent)
        if not getattr(result, "ok", False):
            continue
        xml = result.data.decode("utf-8", errors="replace")
        url_pairs, subs = _parse(xml)
        for url, lastmod in url_pairs:
            if in_domain(url, allowed_domain):
                found[url] = lastmod
        for sub in subs:
            if sub not in visited and in_domain(sub, allowed_domain):
                to_visit.append(sub)

    return list(found.items())
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `uv run pytest tests/test_sitemap.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dhbw_scraper/sitemap.py tests/test_sitemap.py
git commit -m "feat: best-effort sitemap discovery with lastmod"
```

---

## Task 10: Phase 1 — crawl orchestration + change detection

**Files:**
- Create: `src/dhbw_scraper/crawl.py`
- Test: `tests/test_crawl.py`

**Interfaces:**
- Consumes: `config`, `storage`, `fetch`, `links.discover_links`, `sitemap.discover`.
- Produces:
  - `seed_site(conn, site, config, fetch_fn, now) -> None` — sitemap `<lastmod>` upsert + seed URL enqueue.
  - `process_url(conn, run_id, site, url_row, config, fetch_fn, raw_cache, now) -> str` — one URL through change detection; returns outcome (`new|changed|unchanged|removed|error|skipped`). This is the unit-tested core.
  - `crawl_site(conn, run_id, site, config, fetch_fn, raw_cache, clock) -> dict` — claim/loop for one site, bounded by `max_pages`; returns per-site counts.
  - `run_fetch(config, run_id, fetch_fn=fetch.fetch, clock=...) -> dict` — top-level; resets `in_progress`, seeds, runs one thread per site.

- [ ] **Step 1: Write the failing test `tests/test_crawl.py`**

```python
from dhbw_scraper import crawl, storage as st
from dhbw_scraper.config import Config, CrawlConfig, ExtractConfig, Site, StorageConfig
from dhbw_scraper.fetch import FetchResult
from pathlib import Path

NOW = "2026-07-14T00:00:00"


def cfg(tmp_path, max_pages=0):
    return Config(
        root=tmp_path,
        sites=[Site("dhbw", "https://www.dhbw.de/home", "www.dhbw.de")],
        crawl=CrawlConfig(True, max_pages, 0.0, False, 1, "all", "ua"),
        extract=ExtractConfig(1, 50),
        storage=StorageConfig(tmp_path / "db.sqlite3", tmp_path / "raw"),
    )


def mem_and_cache(tmp_path):
    conn = st.connect(":memory:")
    st.init_db(conn)
    return conn, st.RawCache(tmp_path / "raw")


def html_result(url, body, etag=None):
    return FetchResult(url, url, 200, "text/html", body, etag=etag)


def test_process_url_new_html_enqueues_links_and_hands_off(tmp_path):
    conn, cache = mem_and_cache(tmp_path)
    c = cfg(tmp_path)
    site = c.sites[0]
    st.enqueue(conn, "https://www.dhbw.de/home", "www.dhbw.de", 0, None, NOW)
    row = st.claim_pending_url(conn, "www.dhbw.de")

    body = b'<html><body><p>' + b'real content ' * 40 + b'</p><a href="/studium">s</a></body></html>'

    def fetch_fn(url, ua, etag=None, last_modified=None):
        return html_result(url, body)

    outcome = crawl.process_url(conn, "run1", site, row, c, fetch_fn, cache, NOW)
    assert outcome == "new"
    assert st.count_pending(conn, "www.dhbw.de") == 1     # /studium enqueued
    assert st.claim_pending_raw(conn) is not None          # handed off for extraction


def test_process_url_304_is_unchanged(tmp_path):
    conn, cache = mem_and_cache(tmp_path)
    c = cfg(tmp_path)
    st.enqueue(conn, "https://www.dhbw.de/home", "www.dhbw.de", 0, None, NOW)
    st.mark_url_checked(conn, "https://www.dhbw.de/home", 200, '"e"', None, "c1", True, True, NOW)
    conn.execute("UPDATE queue SET work_state='pending' WHERE url=?", ("https://www.dhbw.de/home",))
    conn.commit()
    row = st.claim_pending_url(conn, "www.dhbw.de")

    def fetch_fn(url, ua, etag=None, last_modified=None):
        assert etag == '"e"'
        return FetchResult(url, url, 304, "", b"")

    assert crawl.process_url(conn, "run1", c.sites[0], row, c, fetch_fn, cache, NOW) == "unchanged"
    assert st.claim_pending_raw(conn) is None              # nothing re-extracted


def test_process_url_404_marks_removed(tmp_path):
    conn, cache = mem_and_cache(tmp_path)
    c = cfg(tmp_path)
    st.enqueue(conn, "https://www.dhbw.de/gone", "www.dhbw.de", 0, None, NOW)
    st.upsert_document(conn, "https://www.dhbw.de/gone", "www.dhbw.de", "html", "c1",
                       {"text": "x " * 60, "markdown": "x", "word_count": 60, "metadata": None}, NOW)
    row = st.claim_pending_url(conn, "www.dhbw.de")

    def fetch_fn(url, ua, etag=None, last_modified=None):
        return FetchResult(url, url, 404, "", b"", error="HTTP 404")

    assert crawl.process_url(conn, "run1", c.sites[0], row, c, fetch_fn, cache, NOW) == "removed"
    doc = conn.execute("SELECT present FROM documents WHERE url=?", ("https://www.dhbw.de/gone",)).fetchone()
    assert doc["present"] == 0


def test_crawl_site_respects_max_pages(tmp_path):
    conn, cache = mem_and_cache(tmp_path)
    c = cfg(tmp_path, max_pages=1)
    st.enqueue(conn, "https://www.dhbw.de/a", "www.dhbw.de", 0, None, NOW)
    st.enqueue(conn, "https://www.dhbw.de/b", "www.dhbw.de", 0, None, NOW)

    def fetch_fn(url, ua, etag=None, last_modified=None):
        return html_result(url, b"<html><body><p>" + b"content " * 60 + b"</p></body></html>")

    counts = crawl.crawl_site(conn, "run1", c.sites[0], c, fetch_fn, cache, lambda: NOW)
    assert counts["fetched"] == 1
    assert st.count_pending(conn, "www.dhbw.de") == 1      # second URL untouched
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/test_crawl.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Write `src/dhbw_scraper/crawl.py`**

```python
"""Phase 1: queue-driven crawl with conditional-GET change detection."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import fetch as fetchmod
from . import sitemap, storage
from .links import discover_links


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def seed_site(conn, site, config, fetch_fn, now) -> None:
    if config.crawl.use_sitemap:
        for url, lastmod in sitemap.discover(
            site.seed_url, site.allowed_domain, fetch_fn, config.crawl.user_agent
        ):
            storage.set_sitemap_lastmod(conn, url, site.allowed_domain, lastmod, now)
    storage.enqueue(conn, site.seed_url, site.allowed_domain, 0, None, now)


def process_url(conn, run_id, site, url_row, config, fetch_fn, raw_cache, now) -> str:
    url = url_row["url"]
    result = fetch_fn(
        url, config.crawl.user_agent,
        etag=url_row["etag"], last_modified=url_row["last_modified"],
    )

    if result.not_modified:
        storage.mark_url_checked(conn, url, 304, url_row["etag"],
                                 url_row["last_modified"], url_row["content_sha256"],
                                 False, True, now)
        storage.record_fetch(conn, run_id, url, url, site.allowed_domain, 304,
                             None, url_row["content_sha256"], 0, None, "unchanged", None, now)
        return "unchanged"

    if not result.ok:
        if result.status in (404, 410):
            storage.mark_url_removed(conn, url, now)
            storage.mark_document_removed(conn, url, now)
            storage.record_fetch(conn, run_id, url, url, site.allowed_domain,
                                 result.status, None, None, 0, None, "removed",
                                 result.error, now)
            return "removed"
        storage.mark_url_error(conn, url, result.status, now)
        storage.record_fetch(conn, run_id, url, url, site.allowed_domain,
                             result.status, None, None, 0, None, "error", result.error, now)
        return "error"

    kind = fetchmod.classify(result.content_type, result.final_url)
    if kind == "other":
        storage.mark_url_checked(conn, url, 200, result.etag, result.last_modified,
                                 url_row["content_sha256"], False, True, now)
        storage.record_fetch(conn, run_id, url, result.final_url, site.allowed_domain,
                             200, result.content_type, None, len(result.data),
                             "other", "skipped", None, now)
        return "skipped"

    digest = storage.sha256_bytes(result.data)
    prior = url_row["content_sha256"]
    changed = digest != prior
    outcome = "new" if prior is None else ("changed" if changed else "unchanged")

    storage.mark_url_checked(conn, url, 200, result.etag, result.last_modified,
                             digest, changed, True, now)

    if kind == "html":
        html_text = result.data.decode("utf-8", errors="replace")
        for link in discover_links(html_text, result.final_url, site.allowed_domain):
            storage.enqueue(conn, link, site.allowed_domain, url_row["depth"] + 1, url, now)

    if changed:
        _, path = raw_cache.write(result.data, fetchmod.ext_for(kind))
        rel = str(path)
        storage.upsert_raw_doc(conn, digest, kind, rel, len(result.data), now)

    storage.record_fetch(conn, run_id, url, result.final_url, site.allowed_domain,
                         200, result.content_type, digest, len(result.data),
                         kind, outcome, None, now)
    return outcome


def crawl_site(conn, run_id, site, config, fetch_fn, raw_cache, clock=_now) -> dict:
    counts = {"fetched": 0, "new": 0, "changed": 0, "unchanged": 0,
              "removed": 0, "error": 0, "skipped": 0}
    max_pages = config.crawl.max_pages
    delay = config.crawl.request_delay_seconds
    while max_pages <= 0 or counts["fetched"] < max_pages:
        row = storage.claim_pending_url(conn, site.allowed_domain)
        if row is None:
            break
        outcome = process_url(conn, run_id, site, row, config, fetch_fn, raw_cache, clock())
        counts["fetched"] += 1
        counts[outcome] = counts.get(outcome, 0) + 1
        if delay > 0:
            time.sleep(delay)
    return counts


def run_fetch(config, run_id, fetch_fn=fetchmod.fetch, clock=_now) -> dict:
    conn = storage.connect(config.storage.db_file)
    storage.init_db(conn)
    storage.reset_in_progress(conn)
    raw_cache = storage.RawCache(config.storage.raw_dir)
    for site in config.sites:
        seed_site(conn, site, config, fetch_fn, clock())
    conn.close()

    results: dict[str, dict] = {}
    lock = threading.Lock()

    def worker(site):
        c = storage.connect(config.storage.db_file)
        cache = storage.RawCache(config.storage.raw_dir)
        try:
            counts = crawl_site(c, run_id, site, config, fetch_fn, cache, clock)
        finally:
            c.close()
        with lock:
            results[site.name] = counts

    with ThreadPoolExecutor(max_workers=max(1, len(config.sites))) as ex:
        list(ex.map(worker, config.sites))
    return results
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `uv run pytest tests/test_crawl.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/dhbw_scraper/crawl.py tests/test_crawl.py
git commit -m "feat: phase 1 crawl with conditional-GET change detection"
```

---

## Task 11: Phase 2 — extraction orchestration + materialize documents

**Files:**
- Create: `src/dhbw_scraper/extract.py`
- Test: `tests/test_extract.py`

**Interfaces:**
- Consumes: `storage`, `quality.evaluate`, `html_extract.extract_html`, `pdf_extract.extract_pdf`, `config`.
- Produces:
  - `extract_one(conn, raw_row, config, extractors, now) -> str` — extract → gate → save → materialize documents for all present URLs on that content. Returns `"indexed"|"rejected"|"error"`. `extractors` is `{"html": fn(bytes)->doc|None, "pdf": fn(bytes)->doc|None}` (injectable).
  - `run_extract(config, extractors=None) -> dict` — claim/loop with a thread pool of `config.extract.workers`.

- [ ] **Step 1: Write the failing test `tests/test_extract.py`**

```python
from pathlib import Path

from dhbw_scraper import extract, storage as st
from dhbw_scraper.config import Config, CrawlConfig, ExtractConfig, Site, StorageConfig

NOW = "2026-07-14T00:00:00"


def cfg(tmp_path):
    return Config(
        root=tmp_path,
        sites=[Site("dhbw", "https://www.dhbw.de", "www.dhbw.de")],
        crawl=CrawlConfig(True, 0, 0.0, False, 1, "all", "ua"),
        extract=ExtractConfig(2, 50),
        storage=StorageConfig(tmp_path / "db.sqlite3", tmp_path / "raw"),
    )


def setup_raw(tmp_path, digest="c1", body=b"<html>x</html>", kind="html"):
    conn = st.connect(":memory:")
    st.init_db(conn)
    cache = st.RawCache(tmp_path / "raw")
    _, path = cache.write(body, ".html" if kind == "html" else ".pdf")
    st.upsert_raw_doc(conn, digest, kind, str(path), len(body), NOW)
    # a present URL pointing at this content
    st.enqueue(conn, "https://www.dhbw.de/a", "www.dhbw.de", 0, None, NOW)
    st.mark_url_checked(conn, "https://www.dhbw.de/a", 200, None, None, digest, True, True, NOW)
    return conn, cache


def good_doc(_bytes):
    text = "This is genuinely useful DHBW content. " * 10
    return {"title": "T", "text": text, "markdown": text, "lang": "de",
            "word_count": len(text.split()), "metadata": {"x": 1}}


def test_extract_one_indexes_present_urls(tmp_path):
    conn, _ = setup_raw(tmp_path)
    row = st.claim_pending_raw(conn)
    outcome = extract.extract_one(conn, row, cfg(tmp_path),
                                  {"html": good_doc, "pdf": good_doc}, NOW)
    assert outcome == "indexed"
    doc = conn.execute("SELECT * FROM documents WHERE url='https://www.dhbw.de/a'").fetchone()
    assert doc is not None and doc["word_count"] > 50
    assert st.get_url_state(conn, "https://www.dhbw.de/a")  # sanity


def test_extract_one_rejects_low_quality(tmp_path):
    conn, _ = setup_raw(tmp_path)
    row = st.claim_pending_raw(conn)
    outcome = extract.extract_one(conn, row, cfg(tmp_path),
                                  {"html": lambda b: {"text": "too short", "markdown": "x",
                                                       "word_count": 2, "metadata": None}}, NOW)
    assert outcome == "rejected"
    assert conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"] == 0
    raw = conn.execute("SELECT extract_state, reject_reason FROM raw_docs").fetchone()
    assert raw["extract_state"] == "rejected" and "short" in raw["reject_reason"]
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/test_extract.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Write `src/dhbw_scraper/extract.py`**

```python
"""Phase 2: extract cached content, quality-gate it, materialize documents."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import html_extract, pdf_extract, quality, storage


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _default_extractors():
    return {
        "html": lambda data: html_extract.extract_html(
            data.decode("utf-8", errors="replace")
        ),
        "pdf": pdf_extract.extract_pdf,
    }


def extract_one(conn, raw_row, config, extractors, now) -> str:
    digest = raw_row["content_sha256"]
    source_type = raw_row["source_type"]
    try:
        data = open(raw_row["raw_path"], "rb").read()
        doc = extractors[source_type](data)
    except Exception as exc:  # noqa: BLE001 - one bad doc must not kill the pool
        storage.save_extraction(conn, digest, None, False, None, str(exc), now)
        return "error"

    accepted, reason = quality.evaluate(doc, config.extract.min_words)
    storage.save_extraction(conn, digest, doc, accepted,
                            None if accepted else reason, None, now)
    if not accepted:
        return "rejected"

    for url_row in storage.urls_for_content(conn, digest):
        storage.upsert_document(conn, url_row["url"], url_row["site"], source_type,
                                digest, doc, now)
    return "indexed"


def run_extract(config, extractors=None, clock=_now) -> dict:
    extractors = extractors or _default_extractors()
    counts = {"indexed": 0, "rejected": 0, "error": 0}
    lock = threading.Lock()

    def worker():
        conn = storage.connect(config.storage.db_file)
        try:
            while True:
                row = storage.claim_pending_raw(conn)
                if row is None:
                    return
                outcome = extract_one(conn, row, config, extractors, clock())
                with lock:
                    counts[outcome] = counts.get(outcome, 0) + 1
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=max(1, config.extract.workers)) as ex:
        for _ in range(max(1, config.extract.workers)):
            ex.submit(worker)
    return counts
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `uv run pytest tests/test_extract.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/dhbw_scraper/extract.py tests/test_extract.py
git commit -m "feat: phase 2 extraction, quality gate, and document materialization"
```

---

## Task 12: Progress reporter

**Files:**
- Create: `src/dhbw_scraper/progress.py`
- Test: `tests/test_progress.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Progress(stream, is_tty: bool)` with `.header(text)`, `.update(counts: dict, current: str)`, `.note(text)`, `.summary(title, counts: dict)`. On a TTY it rewrites a single line with `\r`; otherwise it emits periodic plain lines. All output goes to the injected `stream` (default `sys.stderr`).

- [ ] **Step 1: Write the failing test `tests/test_progress.py`**

```python
import io

from dhbw_scraper.progress import Progress


def test_non_tty_emits_plain_lines():
    buf = io.StringIO()
    p = Progress(stream=buf, is_tty=False)
    p.header("Crawling www.dhbw.de")
    p.update({"fetched": 3, "new": 1}, "https://www.dhbw.de/a")
    p.summary("Done", {"fetched": 3, "new": 1})
    out = buf.getvalue()
    assert "Crawling www.dhbw.de" in out
    assert "fetched" in out and "3" in out
    assert "\r" not in out            # no carriage returns when not a TTY


def test_tty_uses_carriage_return():
    buf = io.StringIO()
    p = Progress(stream=buf, is_tty=True)
    p.update({"fetched": 1}, "https://www.dhbw.de/a")
    assert "\r" in buf.getvalue()
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/test_progress.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Write `src/dhbw_scraper/progress.py`**

```python
"""Plain-stderr progress reporting: TTY status line or piped plain lines."""

from __future__ import annotations

import sys


def _fmt(counts: dict) -> str:
    return " | ".join(f"{k} {v}" for k, v in counts.items())


class Progress:
    def __init__(self, stream=None, is_tty: bool | None = None) -> None:
        self.stream = stream or sys.stderr
        self.is_tty = self.stream.isatty() if is_tty is None else is_tty

    def header(self, text: str) -> None:
        self.stream.write(f"\n── {text} ──\n")
        self.stream.flush()

    def update(self, counts: dict, current: str) -> None:
        line = f"{_fmt(counts)} → {current[:60]}"
        if self.is_tty:
            self.stream.write("\r\033[K" + line)
        else:
            self.stream.write(line + "\n")
        self.stream.flush()

    def note(self, text: str) -> None:
        prefix = "\n" if self.is_tty else ""
        self.stream.write(f"{prefix}  ↳ {text}\n")
        self.stream.flush()

    def summary(self, title: str, counts: dict) -> None:
        if self.is_tty:
            self.stream.write("\n")
        self.stream.write(f"\n{title}: {_fmt(counts)}\n")
        self.stream.flush()
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `uv run pytest tests/test_progress.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/dhbw_scraper/progress.py tests/test_progress.py
git commit -m "feat: plain-stderr progress reporter"
```

---

## Task 13: CLI wiring (`fetch`, `extract`, `run`, `stats`, `delta`)

**Files:**
- Create: `src/dhbw_scraper/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `config.load_config`, `crawl.run_fetch`, `extract.run_extract`, `storage`.
- Produces: `build_parser() -> argparse.ArgumentParser`; `main(argv: list[str] | None = None) -> int`. Subcommands: `fetch [--max-pages N] [--config P]`, `extract [--workers N] [--config P]`, `run`, `stats`, `delta --since ISO`.

- [ ] **Step 1: Write the failing test `tests/test_cli.py`**

```python
import json

from dhbw_scraper import cli


def test_parser_has_all_subcommands():
    p = cli.build_parser()
    # argparse exits on unknown; parse each known command
    for cmd in (["fetch"], ["extract"], ["run"], ["stats"], ["delta", "--since", "2026-01-01"]):
        ns = p.parse_args(cmd)
        assert ns.command == cmd[0]


def test_stats_command_prints_counts(tmp_path, capsys, monkeypatch):
    # minimal config on disk
    (tmp_path / "config.toml").write_text(
        """
[[sites]]
name = "x"
seed_url = "https://x/"
allowed_domain = "x"
[crawl]
user_agent = "ua"
[extract]
[storage]
db_file = "db.sqlite3"
raw_dir = "raw"
""",
        encoding="utf-8",
    )
    rc = cli.main(["--config", str(tmp_path / "config.toml"), "stats"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "documents" in out
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Write `src/dhbw_scraper/cli.py`**

```python
"""Command-line entrypoint: fetch / extract / run / stats / delta."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from . import crawl, extract, storage
from .config import load_config


def _run_id() -> str:
    return time.strftime("run-%Y%m%dT%H%M%S", time.gmtime())


def _load(args):
    return load_config(Path(args.config) if args.config else None)


def _cmd_fetch(args) -> int:
    config = _load(args)
    if args.max_pages is not None:
        object.__setattr__(config.crawl, "max_pages", args.max_pages)
    results = crawl.run_fetch(config, _run_id())
    for site, counts in results.items():
        print(f"[{site}] " + " ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


def _cmd_extract(args) -> int:
    config = _load(args)
    if args.workers is not None:
        object.__setattr__(config.extract, "workers", args.workers)
    counts = extract.run_extract(config)
    print(" ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


def _cmd_run(args) -> int:
    rc = _cmd_fetch(args)
    return rc or _cmd_extract(args)


def _cmd_stats(args) -> int:
    config = _load(args)
    conn = storage.connect(config.storage.db_file)
    storage.init_db(conn)
    print(json.dumps(storage.stats(conn), indent=2))
    conn.close()
    return 0


def _cmd_delta(args) -> int:
    config = _load(args)
    conn = storage.connect(config.storage.db_file)
    storage.init_db(conn)
    print(json.dumps(storage.delta(conn, args.since), indent=2, ensure_ascii=False))
    conn.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dhbw-scraper",
                                     description="Incremental DHBW dual-site scraper.")
    parser.add_argument("--config", default=None, help="Path to config.toml.")
    sub = parser.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch", help="Phase 1: crawl + download.")
    f.add_argument("--max-pages", type=int, default=None)
    f.set_defaults(func=_cmd_fetch)

    e = sub.add_parser("extract", help="Phase 2: extract + quality-gate.")
    e.add_argument("--workers", type=int, default=None)
    e.set_defaults(func=_cmd_extract)

    r = sub.add_parser("run", help="fetch then extract.")
    r.add_argument("--max-pages", type=int, default=None)
    r.add_argument("--workers", type=int, default=None)
    r.set_defaults(func=_cmd_run)

    s = sub.add_parser("stats", help="Print DB counts.")
    s.set_defaults(func=_cmd_stats)

    d = sub.add_parser("delta", help="Emit re-index delta since a timestamp.")
    d.add_argument("--since", required=True)
    d.set_defaults(func=_cmd_delta)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
```

Note: `config` dataclasses are frozen; `object.__setattr__` is the intentional escape hatch for CLI overrides. (Acceptable given the values are used immediately and not shared.)

- [ ] **Step 4: Run the tests, verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -v`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add src/dhbw_scraper/cli.py tests/test_cli.py
git commit -m "feat: CLI with fetch/extract/run/stats/delta"
```

---

## Task 14: README + data dir + end-to-end smoke against live sites (bounded)

**Files:**
- Modify: `README.md`
- Create: `data/.gitkeep`, `.gitignore` entries for `data/raw/` and `data/*.sqlite3`
- Test: manual bounded smoke run

**Interfaces:** none (documentation + verification).

- [ ] **Step 1: Update `.gitignore`**

Add:
```
data/raw/
data/*.sqlite3
data/*.sqlite3-wal
data/*.sqlite3-shm
```

- [ ] **Step 2: Create `data/.gitkeep`** (empty file).

- [ ] **Step 3: Rewrite `README.md`**

Document: purpose (RAG stage 1, two sites, incremental), setup (`nix develop && uv sync`, first-run Docling model download + torch weight, nixpkgs fallback), usage (`uv run dhbw-scraper run --max-pages 5`, then `stats`, then `delta --since`), the schema (four tables), the change-detection model, and the robots.txt policy note. Include the `user_agent` contact reminder.

- [ ] **Step 4: Bounded live smoke run**

Docling (PDF extraction) is an optional extra — install it first:
```bash
uv sync --extra pdf   # pulls docling + torch; downloads models on first extract
```

Run:
```bash
uv run dhbw-scraper fetch --max-pages 5
uv run dhbw-scraper extract
uv run dhbw-scraper stats
```
Expected: `stats` shows `documents > 0`, `queue_pending` reflecting the frontier, and no tracebacks. Then re-run `fetch --max-pages 5` and confirm the log shows `unchanged` outcomes (304 / same-hash), proving change detection works.

- [ ] **Step 5: Commit**

```bash
git add README.md data/.gitkeep .gitignore
git commit -m "docs: README, data dir, gitignore; verified bounded end-to-end run"
```

---

## Self-Review notes

- **Spec coverage:** two sites strict per-domain (Task 1 config, Task 5 `in_domain`, Task 10 crawl); sitemap seeding (Task 9, wired in Task 10 `seed_site`); link following (Task 5/10); PDFs via Docling (Task 8); trafilatura HTML (Task 7); moderate quality gate (Task 6, applied Task 11); SQLite four-table model + WAL + atomic claims (Tasks 2–3); two-phase fetch/extract (Tasks 10–11); parallel per-host fetch + extractor pool (Tasks 10–11); change detection conditional-GET + content-hash + sitemap-lastmod (Tasks 4/9/10); deletions via 404 → `present=0` (Task 10); delta for re-index (Task 3, exposed Task 13); progress on stderr (Task 12); CLI incl. `stats`/`delta` (Task 13); README + Docling caveat (Task 14).
- **Ignored-robots:** enforced by never constructing a robots gate; `respect_robots` retained in config only for auditability/future use.
- **Type consistency:** the extractor doc dict shape `{title, text, markdown, lang, word_count, metadata}` is produced by Tasks 7/8, consumed by Tasks 6/11; `storage.upsert_document` requires `text`, `markdown`, `word_count` (always present). `FetchResult` fields used in Tasks 9/10 match Task 4.
- **Known simplification:** `--changed-only` / `--full` re-check flags from the spec are represented by `config.crawl.recheck` but not wired to distinct fetch behavior in this plan (default `all`: every claimed URL is conditionally re-fetched, which is already cheap via 304). If you want the flags as CLI switches, add a follow-up task; noted here so it isn't a silent gap.
