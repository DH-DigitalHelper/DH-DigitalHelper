"""SQLite persistence: schema, atomic claims, dedup, upserts, delta, raw cache."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from itertools import groupby
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from . import classify, lang, pdf_title, taxonomy

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
-- Covers claim_pending_url's hot query end to end (site + state filter, then
-- depth,url order) so the claim is a single index seek and the IMMEDIATE write
-- lock is held for microseconds instead of scanning under 192 concurrent workers.
CREATE INDEX IF NOT EXISTS idx_queue_claim ON queue(site, work_state, depth, url);
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
-- Covers the per-type claim (WHERE source_type=? AND extract_state=? ORDER BY
-- first_seen_at) as a single index seek + ordered scan, so extract-html and
-- extract-pdf never fall back to a filter+sort over all raw_docs.
CREATE INDEX IF NOT EXISTS idx_raw_claim ON raw_docs(source_type, extract_state, first_seen_at);

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
    -- SHA-256 of the NORMALIZED extracted text (see text_hash). The corpus is
    -- deduplicated on this so byte-different / text-identical pages (e.g. TYPO3
    -- cHash URL variants) collapse to a single canonical document. Nullable so
    -- an ALTER-added column on a pre-existing DB is backfilled by `dedup`.
    text_sha256      TEXT,
    present          INTEGER NOT NULL DEFAULT 1,
    revision         INTEGER NOT NULL DEFAULT 1,
    first_indexed_at TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_site ON documents(site);
CREATE INDEX IF NOT EXISTS idx_documents_updated ON documents(updated_at);
CREATE INDEX IF NOT EXISTS idx_documents_present ON documents(present);
-- idx_documents_text_sha256 is created by _migrate(), not here: on a pre-existing
-- DB the documents table lacks text_sha256 until _migrate's ALTER adds it, so a
-- CREATE INDEX in this batch (which runs before _migrate) would fail.

-- Derived RAG corpus.  The document columns that are useful as retrieval filters
-- are copied onto each chunk, while document_id keeps the canonical relationship.
-- `chunk` can rebuild this table without changing the source documents.
CREATE TABLE IF NOT EXISTS document_chunks (
    id                   TEXT PRIMARY KEY,
    document_id          TEXT NOT NULL,
    chunk_index          INTEGER NOT NULL,
    url                  TEXT NOT NULL,
    title                TEXT,
    site                 TEXT NOT NULL,
    source_type          TEXT NOT NULL,
    lang                 TEXT,
    text                 TEXT NOT NULL,
    markdown             TEXT NOT NULL,
    heading_path         TEXT NOT NULL,
    word_count           INTEGER NOT NULL,
    char_count           INTEGER NOT NULL,
    content_sha256       TEXT NOT NULL,
    document_text_sha256 TEXT NOT NULL,
    document_content_sha256 TEXT NOT NULL,
    document_metadata_sha256 TEXT NOT NULL,
    document_revision    INTEGER NOT NULL,
    metadata             TEXT,
    standort_id          INTEGER,
    department_id        INTEGER,
    study_program_id     INTEGER,
    classify_meta        TEXT,
    chunker_version      INTEGER NOT NULL,
    target_words         INTEGER NOT NULL,
    overlap_words        INTEGER NOT NULL,
    created_at           TEXT NOT NULL,
    UNIQUE (document_id, chunk_index),
    FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chunks_document ON document_chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_site ON document_chunks(site);
CREATE INDEX IF NOT EXISTS idx_chunks_standort ON document_chunks(standort_id);
CREATE INDEX IF NOT EXISTS idx_chunks_department ON document_chunks(department_id);
CREATE INDEX IF NOT EXISTS idx_chunks_study_program
    ON document_chunks(study_program_id);
CREATE INDEX IF NOT EXISTS idx_chunks_content_sha256 ON document_chunks(content_sha256);

-- URL dictionary: every distinct endpoint that appears in `links`, stored once.
-- `links` references these ids (no declared FK, matching the rest of the schema,
-- e.g. documents.content_sha256). Written by the Rust Phase-1 writer's interner and
-- read back by the Phase-2 dashboard, which JOINs `urls` to recover src/dst text.
CREATE TABLE IF NOT EXISTS urls (
    id  INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE
);

-- Outbound link graph: every <a href> a crawled page emits, in-domain and external
-- alike. src_id/dst_id reference urls(id) (interned to cut the multi-million-edge
-- graph from storing full URL text twice per row + in the PK + dst index). Written by
-- the Rust Phase-1 crawler (see src/scrape-engine/storage.rs, which keeps this DDL in
-- sync). Following stays in-domain (in_domain=1 marks a follow candidate);
-- external/cross-campus edges are recorded, never crawled. Read back by the Phase-2
-- dashboard via urls JOINs. queue.discovered_from keeps the first-discoverer.
CREATE TABLE IF NOT EXISTS links (
    src_id        INTEGER NOT NULL,
    dst_id        INTEGER NOT NULL,
    site          TEXT NOT NULL,
    in_domain     INTEGER NOT NULL,
    depth         INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (src_id, dst_id)
);
CREATE INDEX IF NOT EXISTS idx_links_dst  ON links(dst_id);
CREATE INDEX IF NOT EXISTS idx_links_site ON links(site);

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
"""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_WS = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Canonicalize extracted text for hashing via Unicode NFC, collapsing whitespace runs to a single space and stripping."""
    return _WS.sub(" ", unicodedata.normalize("NFC", text or "")).strip()


def text_hash(text: str) -> str:
    """SHA-256 of the normalized extracted text -- the dedup key for the corpus."""
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def _canonical_key(url: str):
    """Order URLs so the cleanest one sorts smallest: fewest query params, then shortest, then lexicographically."""
    query = urlsplit(url).query
    return (len(parse_qsl(query, keep_blank_values=True)), len(url), url)


def _is_locked_error(exc: BaseException) -> bool:
    """True only for SQLite write-lock contention, never for a genuine schema/SQL error."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _retry_locked(fn, retries=4, base_delay=0.05, sleep=time.sleep):
    """Call fn(), retrying only on a transient write-lock error with a short growing backoff."""
    for attempt in range(retries + 1):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if attempt >= retries or not _is_locked_error(exc):
                raise
            sleep(base_delay * (2**attempt))


def connect(db_file: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_file), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply forward-only, idempotent migrations to an existing DB."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
    if cols and "text_sha256" not in cols:
        conn.execute("ALTER TABLE documents ADD COLUMN text_sha256 TEXT")
    for col in ("standort_id", "department_id", "study_program_id"):
        if cols and col not in cols:
            conn.execute(f"ALTER TABLE documents ADD COLUMN {col} INTEGER")
    if cols and "classify_meta" not in cols:
        conn.execute("ALTER TABLE documents ADD COLUMN classify_meta TEXT")
    chunk_cols = {r["name"] for r in conn.execute("PRAGMA table_info(document_chunks)")}
    for col in ("document_content_sha256", "document_metadata_sha256"):
        if chunk_cols and col not in chunk_cols:
            conn.execute(f"ALTER TABLE document_chunks ADD COLUMN {col} TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_standort ON documents(standort_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_department ON documents(department_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_text_sha256 "
        "ON documents(text_sha256, present)"
    )


def _seed_taxonomy(conn) -> None:
    """Seed the fixed vocabularies (idempotent)."""
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


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    _seed_taxonomy(conn)
    conn.commit()


@contextmanager
def write_txn(conn):
    """Run a batch of writes under one IMMEDIATE transaction instead of committing each statement separately."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _enqueue_many(conn, rows) -> int:
    """Insert many queue rows in one statement (no commit), returning the number newly inserted."""
    cur = conn.executemany(
        """INSERT OR IGNORE INTO queue (url, site, depth, discovered_from, first_seen_at)
           VALUES (?, ?, ?, ?, ?)""",
        rows,
    )
    return cur.rowcount


def enqueue_many(conn, rows) -> int:
    added = _enqueue_many(conn, rows)
    conn.commit()
    return added


def enqueue(conn, url, site, depth, discovered_from, now) -> bool:
    cur = conn.execute(
        """INSERT OR IGNORE INTO queue (url, site, depth, discovered_from, first_seen_at)
           VALUES (?, ?, ?, ?, ?)""",
        (url, site, depth, discovered_from, now),
    )
    conn.commit()
    return cur.rowcount > 0


def set_sitemap_lastmod(conn, url, site, lastmod, now) -> None:
    """Record a sitemap <lastmod> value for a URL and re-queue it if the value genuinely advanced."""
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
        stored = row["sitemap_lastmod"]
        if lastmod is not None and stored is not None and lastmod > stored:
            conn.execute(
                "UPDATE queue SET sitemap_lastmod = ?, work_state = 'pending' WHERE url = ?",
                (lastmod, url),
            )
        elif lastmod is not None and stored is None:
            conn.execute(
                "UPDATE queue SET sitemap_lastmod = ? WHERE url = ?",
                (lastmod, url),
            )
    conn.commit()


def requeue_present_urls(conn, site) -> int:
    """Reset all present, already-fetched URLs for a site back to 'pending' so a re-check run conditionally re-fetches them."""
    cur = conn.execute(
        "UPDATE queue SET work_state = 'pending' "
        "WHERE site = ? AND present = 1 AND work_state = 'done'",
        (site,),
    )
    conn.commit()
    return cur.rowcount


def requeue_transient_errors(conn, site) -> int:
    """Flip error rows whose stored HTTP status is transient back to 'pending' so the next run retries them."""
    cur = conn.execute(
        "UPDATE queue SET work_state = 'pending' "
        "WHERE site = ? AND work_state = 'error' "
        "AND (http_status = 0 OR http_status = 408 OR http_status = 429 "
        "OR (http_status >= 500 AND http_status < 600))",
        (site,),
    )
    conn.commit()
    return cur.rowcount


def reset_in_progress(conn) -> int:
    cur = conn.execute(
        "UPDATE queue SET work_state = 'pending' WHERE work_state = 'in_progress'"
    )
    conn.commit()
    return cur.rowcount


_SITE_SCOPED_TABLES = ("queue", "crawl_log", "documents", "links")


def reset_site(conn, site) -> dict:
    """Hard-delete all per-site crawl state for a site so the next crawl re-seeds and rebuilds it from scratch."""
    counts: dict = {}
    with write_txn(conn):
        for table in _SITE_SCOPED_TABLES:
            cur = conn.execute(f"DELETE FROM {table} WHERE site = ?", (site,))
            counts[table] = cur.rowcount
    return counts


def _claim_pending_url(conn, site, only_new=False) -> sqlite3.Row | None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        extra = " AND last_checked_at IS NULL" if only_new else ""
        row = conn.execute(
            "SELECT * FROM queue WHERE site = ? AND work_state = 'pending'"
            + extra
            + " ORDER BY depth, url LIMIT 1",
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


def claim_pending_url(conn, site, only_new=False) -> sqlite3.Row | None:
    return _retry_locked(lambda: _claim_pending_url(conn, site, only_new=only_new))


def requeue_url(conn, url) -> None:
    """Flip a single URL back to 'pending', retrying on transient write-lock contention."""

    def _do():
        conn.execute("UPDATE queue SET work_state='pending' WHERE url=?", (url,))
        conn.commit()

    _retry_locked(_do)


def get_url_state(conn, url) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM queue WHERE url = ?", (url,)).fetchone()


def count_pending(conn, site=None, only_new=False) -> int:
    extra = " AND last_checked_at IS NULL" if only_new else ""
    if site is None:
        row = conn.execute(
            "SELECT COUNT(*) c FROM queue WHERE work_state = 'pending'" + extra
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) c FROM queue WHERE work_state = 'pending' AND site = ?"
            + extra,
            (site,),
        ).fetchone()
    return row["c"]


def count_pending_raw(conn, source_type=None) -> int:
    sql = "SELECT COUNT(*) c FROM raw_docs WHERE extract_state = 'pending'"
    params: tuple = ()
    if source_type is not None:
        sql += " AND source_type = ?"
        params = (source_type,)
    row = conn.execute(sql, params).fetchone()
    return row["c"]


class RawCache:
    """Content-addressed store for downloaded bytes under root."""

    def __init__(self, root) -> None:
        self.root = Path(root)

    def path_for(self, digest: str, ext: str) -> Path:
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


def _record_fetch(
    conn,
    run_id,
    url,
    final_url,
    site,
    status,
    content_type,
    sha256,
    nbytes,
    kind,
    outcome,
    error,
    now,
) -> None:
    conn.execute(
        """INSERT INTO crawl_log
           (run_id, url, final_url, site, status, content_type, sha256, bytes,
            kind, outcome, error, fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            run_id,
            url,
            final_url,
            site,
            status,
            content_type,
            sha256,
            nbytes,
            kind,
            outcome,
            error,
            now,
        ),
    )


def record_fetch(conn, *args) -> None:
    _record_fetch(conn, *args)
    conn.commit()


def _mark_url_checked(
    conn, url, http_status, etag, last_modified, content_sha256, changed, present, now
) -> None:
    if changed:
        conn.execute(
            """UPDATE queue SET work_state='done', http_status=?, etag=?,
                   last_modified=?, content_sha256=?, present=?,
                   last_checked_at=?, last_changed_at=? WHERE url=?""",
            (
                http_status,
                etag,
                last_modified,
                content_sha256,
                1 if present else 0,
                now,
                now,
                url,
            ),
        )
    else:
        conn.execute(
            """UPDATE queue SET work_state='done', http_status=?, etag=?,
                   last_modified=?, present=?, last_checked_at=? WHERE url=?""",
            (http_status, etag, last_modified, 1 if present else 0, now, url),
        )


def mark_url_checked(conn, *args) -> None:
    _mark_url_checked(conn, *args)
    conn.commit()


def _mark_url_error(conn, url, http_status, now) -> None:
    conn.execute(
        "UPDATE queue SET work_state='error', http_status=?, last_checked_at=? WHERE url=?",
        (http_status, now, url),
    )


def mark_url_error(conn, url, http_status, now) -> None:
    _mark_url_error(conn, url, http_status, now)
    conn.commit()


def _mark_url_removed(conn, url, now) -> None:
    conn.execute(
        """UPDATE queue SET work_state='done', present=0, http_status=404,
               last_checked_at=?, last_changed_at=? WHERE url=?""",
        (now, now, url),
    )


def mark_url_removed(conn, url, now) -> None:
    _mark_url_removed(conn, url, now)
    conn.commit()


def _upsert_raw_doc(conn, content_sha256, source_type, raw_path, nbytes, now) -> bool:
    """Atomically insert a raw_docs row for content_sha256 if one doesn't already exist."""
    cur = conn.execute(
        """INSERT INTO raw_docs (content_sha256, source_type, raw_path, bytes,
               first_seen_at, extract_state)
           VALUES (?,?,?,?,?, 'pending')
           ON CONFLICT(content_sha256) DO NOTHING""",
        (content_sha256, source_type, raw_path, nbytes, now),
    )
    return cur.rowcount == 1


def upsert_raw_doc(conn, content_sha256, source_type, raw_path, nbytes, now) -> bool:
    is_new = _upsert_raw_doc(conn, content_sha256, source_type, raw_path, nbytes, now)
    conn.commit()
    return is_new


def _requeue_extraction(conn, content_sha256, now) -> bool:
    cur = conn.execute(
        "UPDATE raw_docs SET extract_state='pending' WHERE content_sha256=?",
        (content_sha256,),
    )
    return cur.rowcount > 0


def requeue_extraction(conn, content_sha256, now) -> bool:
    """Force re-extraction of an already-seen content blob so Phase 2 re-materializes documents for all URLs pointing at it."""
    updated = _requeue_extraction(conn, content_sha256, now)
    conn.commit()
    return updated


def reset_extract_in_progress(conn, source_type=None) -> int:
    """Reset raw_docs stranded in_progress (from a crashed extract worker) back to pending."""
    sql = "UPDATE raw_docs SET extract_state = 'pending' WHERE extract_state = 'in_progress'"
    params: tuple = ()
    if source_type is not None:
        sql += " AND source_type = ?"
        params = (source_type,)
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.rowcount


def reset_extract_errors(conn, source_type=None) -> int:
    """Re-queue raw_docs whose extraction errored, so the next extract run retries them."""
    sql = (
        "UPDATE raw_docs SET extract_state = 'pending', extract_error = NULL "
        "WHERE extract_state = 'error'"
    )
    params: tuple = ()
    if source_type is not None:
        sql += " AND source_type = ?"
        params = (source_type,)
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.rowcount


def claim_pending_raw(conn, source_type=None) -> sqlite3.Row | None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        sql = "SELECT * FROM raw_docs WHERE extract_state='pending'"
        params: tuple = ()
        if source_type is not None:
            sql += " AND source_type=?"
            params = (source_type,)
        sql += " ORDER BY first_seen_at, content_sha256 LIMIT 1"
        row = conn.execute(sql, params).fetchone()
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


def _save_extraction(
    conn, content_sha256, doc, quality_ok, reject_reason, extract_error, now
) -> None:
    """Write the extract result onto its raw_docs row (no commit)."""
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
        (
            state,
            d.get("title"),
            d.get("text"),
            d.get("markdown"),
            d.get("lang"),
            d.get("word_count"),
            json.dumps(d.get("metadata")) if d.get("metadata") else None,
            1 if quality_ok else 0,
            reject_reason,
            extract_error,
            now,
            content_sha256,
        ),
    )


def save_extraction(
    conn, content_sha256, doc, quality_ok, reject_reason, extract_error, now
) -> None:
    _save_extraction(
        conn, content_sha256, doc, quality_ok, reject_reason, extract_error, now
    )
    conn.commit()


def urls_for_content(conn, content_sha256):
    return conn.execute(
        "SELECT * FROM queue WHERE content_sha256=? AND present=1", (content_sha256,)
    ).fetchall()


def _doc_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


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
    """Classify (url, site, doc) and write the four enrichment columns onto the document row by id."""
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


def _upsert_document(conn, url, site, source_type, content_sha256, doc, now) -> str:
    """Materialize one URL's document row (no commit), deduplicated on the extracted-text hash."""
    h = text_hash(doc["text"])

    others = conn.execute(
        "SELECT url FROM documents WHERE text_sha256=? AND url<>? AND present=1",
        (h, url),
    ).fetchall()
    if others:
        cleanest_other = min(others, key=lambda r: _canonical_key(r["url"]))["url"]
        if _canonical_key(cleanest_other) <= _canonical_key(url):
            _mark_document_removed(conn, url, now)
            return "duplicate"
        for r in others:
            _mark_document_removed(conn, r["url"], now)

    existing = conn.execute("SELECT * FROM documents WHERE url=?", (url,)).fetchone()
    meta = json.dumps(doc.get("metadata")) if doc.get("metadata") else None
    title = doc.get("title") or pdf_title.from_url(url)
    if existing is None:
        conn.execute(
            """INSERT INTO documents (id, url, final_url, site, source_type,
                   content_sha256, title, text, markdown, lang, word_count, metadata,
                   text_sha256, present, revision, first_indexed_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,1,?,?)""",
            (
                _doc_id(url),
                url,
                url,
                site,
                source_type,
                content_sha256,
                title,
                doc["text"],
                doc["markdown"],
                doc.get("lang"),
                doc["word_count"],
                meta,
                h,
                now,
                now,
            ),
        )
        _set_classification(conn, _doc_id(url), url, site, doc)
        return "new"
    if existing["text_sha256"] != h:
        conn.execute(
            """UPDATE documents SET content_sha256=?, source_type=?, title=?, text=?,
                   markdown=?, lang=?, word_count=?, metadata=?, text_sha256=?,
                   present=1, revision=revision+1, updated_at=? WHERE url=?""",
            (
                content_sha256,
                source_type,
                title,
                doc["text"],
                doc["markdown"],
                doc.get("lang"),
                doc["word_count"],
                meta,
                h,
                now,
                url,
            ),
        )
        _set_classification(conn, existing["id"], url, site, doc)
        return "changed"
    if existing["content_sha256"] != content_sha256:
        conn.execute(
            "UPDATE documents SET content_sha256=?, source_type=? WHERE url=?",
            (content_sha256, source_type, url),
        )
    if existing["present"] == 0:
        conn.execute(
            "UPDATE documents SET present=1, updated_at=? WHERE url=?", (now, url)
        )
    else:
        conn.execute("UPDATE documents SET present=1 WHERE url=?", (url,))
    return "unchanged"


def upsert_document(conn, url, site, source_type, content_sha256, doc, now) -> str:
    result = _upsert_document(conn, url, site, source_type, content_sha256, doc, now)
    conn.commit()
    return result


def _mark_document_removed(conn, url, now) -> None:
    """Retire a document from the live corpus (no commit)."""
    conn.execute(
        "UPDATE documents SET present=0, updated_at=? WHERE url=? AND present=1",
        (now, url),
    )


def mark_document_removed(conn, url, now) -> None:
    _mark_document_removed(conn, url, now)
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
    return {
        "upserts": [dict(r) for r in upserts],
        "deletions": [dict(r) for r in deletions],
    }


def stats(conn) -> dict:
    def scalar(q, *a):
        return conn.execute(q, a).fetchone()[0]

    by_reason = conn.execute(
        "SELECT reject_reason, COUNT(*) c FROM raw_docs "
        "WHERE extract_state='rejected' GROUP BY reject_reason"
    ).fetchall()
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
        "WHERE present=1 AND (department_id IS NULL OR classify_meta IS NULL)"
    )
    return {
        "queue_pending": scalar(
            "SELECT COUNT(*) FROM queue WHERE work_state='pending'"
        ),
        "queue_done": scalar("SELECT COUNT(*) FROM queue WHERE work_state='done'"),
        "queue_error": scalar("SELECT COUNT(*) FROM queue WHERE work_state='error'"),
        "urls_present": scalar("SELECT COUNT(*) FROM queue WHERE present=1"),
        "urls_removed": scalar("SELECT COUNT(*) FROM queue WHERE present=0"),
        "raw_pending": scalar(
            "SELECT COUNT(*) FROM raw_docs WHERE extract_state='pending'"
        ),
        "documents": scalar("SELECT COUNT(*) FROM documents WHERE present=1"),
        "documents_removed": scalar("SELECT COUNT(*) FROM documents WHERE present=0"),
        "rejects": {r["reject_reason"]: r["c"] for r in by_reason},
        "by_department": {r["name"]: r["c"] for r in by_department},
        "by_standort": {r["name"]: r["c"] for r in by_standort},
        "unclassified": unclassified,
    }


def run_dedup(
    conn, batch_size: int = 500, vacuum: bool = True, now: str | None = None
) -> dict:
    """Backfill text_sha256 and retire duplicate documents, keeping the single cleanest URL per distinct extracted text."""
    now = now or time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    before = conn.execute(
        "SELECT COUNT(*) c FROM documents WHERE present=1"
    ).fetchone()["c"]

    backfilled = 0
    last_id = ""
    while True:
        rows = conn.execute(
            "SELECT id, text, text_sha256 FROM documents "
            "WHERE id > ? ORDER BY id LIMIT ?",
            (last_id, batch_size),
        ).fetchall()
        if not rows:
            break
        todo = [
            (text_hash(r["text"]), r["id"]) for r in rows if r["text_sha256"] is None
        ]
        if todo:
            with write_txn(conn):
                for h, doc_id in todo:
                    conn.execute(
                        "UPDATE documents SET text_sha256=? WHERE id=?", (h, doc_id)
                    )
            backfilled += len(todo)
        last_id = rows[-1]["id"]

    rows = conn.execute(
        "SELECT id, url, text_sha256 FROM documents "
        "WHERE present=1 AND text_sha256 IS NOT NULL "
        "ORDER BY text_sha256"
    ).fetchall()
    groups = 0
    to_delete: list[str] = []
    for _h, grp in groupby(rows, key=lambda r: r["text_sha256"]):
        members = list(grp)
        if len(members) < 2:
            continue
        groups += 1
        keep = min(members, key=lambda r: _canonical_key(r["url"]))["id"]
        to_delete.extend(m["id"] for m in members if m["id"] != keep)

    deleted = 0
    for i in range(0, len(to_delete), batch_size):
        chunk = to_delete[i : i + batch_size]
        with write_txn(conn):
            for doc_id in chunk:
                conn.execute(
                    "UPDATE documents SET present=0, updated_at=? WHERE id=?",
                    (now, doc_id),
                )
        deleted += len(chunk)

    if vacuum and deleted:
        conn.execute("VACUUM")

    after = conn.execute("SELECT COUNT(*) c FROM documents WHERE present=1").fetchone()[
        "c"
    ]
    return {
        "backfilled": backfilled,
        "groups": groups,
        "deleted": deleted,
        "before": before,
        "after": after,
    }


def run_reclassify(conn, batch_size: int = 500) -> dict:
    """Re-run classify over every document row and rewrite the four enrichment columns."""
    updated = 0
    last_id = ""
    while True:
        rows = conn.execute(
            "SELECT id, url, site, title, metadata FROM documents "
            "WHERE id > ? ORDER BY id LIMIT ?",
            (last_id, batch_size),
        ).fetchall()
        if not rows:
            break
        with write_txn(conn):
            for r in rows:
                doc = {
                    "title": r["title"],
                    "metadata": json.loads(r["metadata"]) if r["metadata"] else None,
                }
                _set_classification(conn, r["id"], r["url"], r["site"], doc)
        updated += len(rows)
        last_id = rows[-1]["id"]
    return {"reclassified": updated}


def _backfill_title(row, cache, ext_for) -> str | None:
    """Best title for a title-less document: a PDF's cleaned embedded metadata title, else the URL basename."""
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
    """One-time repair of the three dead metadata fields over the existing corpus, in one keyset-paginated pass over present documents."""
    from . import fetch as fetchmod

    cache = RawCache(raw_dir)

    doc_keys = {
        (r["url"], r["content_sha256"])
        for r in conn.execute(
            "SELECT url, content_sha256 FROM documents WHERE present=1"
        )
    }
    final_by_key: dict = {}
    for r in conn.execute(
        "SELECT url, sha256, final_url FROM crawl_log "
        "WHERE sha256 IS NOT NULL AND final_url IS NOT NULL AND status = 200 "
        "ORDER BY id"
    ):
        key = (r["url"], r["sha256"])
        if key in doc_keys:
            final_by_key[key] = r["final_url"]

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
        pending = []
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
