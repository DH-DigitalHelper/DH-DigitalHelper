"""SQLite persistence: schema, atomic claims, dedup, upserts, delta, raw cache.

All functions take an explicit connection so each worker (thread/process) uses
its own. The database runs in WAL mode; writers serialise via BEGIN IMMEDIATE.
"""

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

from . import taxonomy

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
    """Canonicalize extracted text for hashing: Unicode NFC, collapse every
    whitespace run (incl. NBSP and other Unicode spaces, which ``\\s`` matches in
    ``str`` mode) to a single space, and strip. Deliberately conservative -- NO
    casefold and NO NFKC, both of which would merge semantically distinct pages
    ("SS 2024" vs "ss 2024", ligatures/full-width forms)."""
    return _WS.sub(" ", unicodedata.normalize("NFC", text or "")).strip()


def text_hash(text: str) -> str:
    """SHA-256 of the normalized extracted text -- the dedup key for the corpus."""
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def _canonical_key(url: str):
    """Order URLs so the *cleanest* one sorts smallest: fewest query params, then
    shortest, then lexicographically. Picks the bare '.../dualis-firmenliste/'
    over every '?...&cHash=...' variant. ``url`` is UNIQUE so the order is total."""
    query = urlsplit(url).query
    return (len(parse_qsl(query, keep_blank_values=True)), len(url), url)


def _is_locked_error(exc: BaseException) -> bool:
    """True only for SQLite write-lock contention ('database is locked' /
    'database is busy'), never for a genuine schema/SQL error."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _retry_locked(fn, retries=4, base_delay=0.05, sleep=time.sleep):
    """Call ``fn()``, retrying only on a transient write-lock error with a
    short growing backoff. The connection's ``busy_timeout`` already blocks
    inside SQLite before raising, so this is a thin second layer for the rare
    lock that surfaces past it. Non-lock errors — and the final attempt —
    propagate unchanged."""
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
    # NORMAL is durable under WAL (no corruption on app/OS crash, only the last
    # transaction may be lost on power loss) and drops the per-commit fsync, so
    # the single WAL write lock is released far sooner under many concurrent
    # workers. The raised busy_timeout lets a genuinely busy moment wait rather
    # than surfacing "database is locked".
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply forward-only, idempotent migrations to an existing DB.

    The repo has no migration framework: every Python entrypoint runs
    ``init_db``, so schema additions live here and are applied lazily. Column
    presence is checked via introspection (not ``user_version``) so this is a
    no-op whether the column arrived via SCHEMA (fresh DB, incl. a Rust-created
    one) or a prior ALTER, and never raises a duplicate-column error.

    ``ALTER TABLE ADD COLUMN`` with a NULL default is an O(1) metadata-only
    change in SQLite -- instant even on a multi-GB file, no table rewrite."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
    if cols and "text_sha256" not in cols:
        conn.execute("ALTER TABLE documents ADD COLUMN text_sha256 TEXT")
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
    # Composite (text_sha256, present): both the dedup lookup in _upsert_document
    # and run_dedup filter on `text_sha256=? AND present=1`. A single-column
    # text_sha256 index is NOT reliably chosen -- SQLite (without ANALYZE stats)
    # will happily service that predicate via the low-selectivity `present` index
    # and scan the whole table per lookup (O(n) each -> O(n^2) overall). The
    # two-equality composite is unambiguously the most selective, so the planner
    # picks it and each lookup is a seek.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_text_sha256 "
        "ON documents(text_sha256, present)"
    )


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


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    _seed_taxonomy(conn)
    conn.commit()


@contextmanager
def write_txn(conn):
    """Run a batch of writes under ONE IMMEDIATE transaction (one write-lock
    acquisition, one fsync) instead of committing each statement separately.

    Compose the non-committing ``_core`` write helpers inside this block so a
    whole page's mutations land atomically and hold the single WAL write lock
    for as short a time as possible. Mirrors ``claim_pending_url``'s explicit
    BEGIN IMMEDIATE / COMMIT / ROLLBACK so the two never nest."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _enqueue_many(conn, rows) -> int:
    """Insert many queue rows in one statement (no commit). Each row is a full
    ``(url, site, depth, discovered_from, first_seen_at)`` tuple; existing URLs
    are left untouched (INSERT OR IGNORE). Returns the number newly inserted."""
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
    """Record a sitemap <lastmod> value for ``url`` and re-queue it if the
    value genuinely advanced.

    A known baseline is never erased or regressed: a missing (``None``)
    lastmod or one that is not strictly greater than the stored value is
    ignored entirely. This matters because these sites emit no ETag/
    Last-Modified headers, so the sitemap-lastmod advance is the primary
    signal that triggers re-checking a URL for changes.
    """
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
        # else: lastmod is None, or lastmod <= stored -> leave baseline untouched.
    conn.commit()


def requeue_present_urls(conn, site) -> int:
    """Reset all present, already-fetched URLs for a site back to 'pending' so a
    re-check run conditionally re-fetches them. Leaves in_progress/error/removed rows alone."""
    cur = conn.execute(
        "UPDATE queue SET work_state = 'pending' "
        "WHERE site = ? AND present = 1 AND work_state = 'done'",
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


# Tables whose rows are keyed per-site and safe to wipe for a clean re-crawl.
# raw_docs is deliberately NOT here: it is content-addressed (keyed by
# content_sha256, not site) and shared across URLs/sites, so wiping it by site is
# both meaningless and wasteful -- keeping it lets a re-crawl reuse the extraction
# cache (identical bytes are never re-extracted).
_SITE_SCOPED_TABLES = ("queue", "crawl_log", "documents", "links")


def reset_site(conn, site) -> dict:
    """Hard-delete all per-site crawl state for ``site`` (its allowed_domain), so
    the next crawl re-seeds and rebuilds it from scratch.

    Removes the site's rows from ``queue`` (frontier + change-detection state),
    ``crawl_log`` (fetch audit), ``documents`` (materialized corpus), and ``links``
    (edge graph), all in one transaction. Leaves the content-addressed ``raw_docs``
    cache intact (see :data:`_SITE_SCOPED_TABLES`). Returns per-table delete counts.
    """
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
    """Flip a single URL back to 'pending' (used to release a claim that could
    not be processed). Retries on transient write-lock contention."""

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
    """Content-addressed store for downloaded bytes under ``root``."""

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
    """Atomically insert a raw_docs row for ``content_sha256`` if one doesn't
    already exist.

    Uses a single INSERT ... ON CONFLICT DO NOTHING statement (rather than a
    separate SELECT-then-INSERT) so that concurrent fetch workers
    (workers_per_host>1) downloading byte-identical content cannot race each
    other into a sqlite3.IntegrityError on the content_sha256 uniqueness
    constraint.

    Returns True iff this call newly inserted the row (extraction starts
    'pending'); False if the digest already existed (the caller is expected
    to call requeue_extraction in that case).
    """
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
    """Force re-extraction of an already-seen content blob so Phase 2
    re-materializes documents for all URLs currently pointing at it
    (used when a removed URL reappears, or a new URL shares existing content).
    Returns True if a row was updated."""
    updated = _requeue_extraction(conn, content_sha256, now)
    conn.commit()
    return updated


def reset_extract_in_progress(conn, source_type=None) -> int:
    """Reset raw_docs stranded in_progress (from a crashed extract worker) back to pending.

    When ``source_type`` is given, only that type's rows are reset -- so an
    ``extract-html`` recovery pass never steals the ``in_progress`` rows a
    concurrently running ``extract-pdf`` pass has legitimately claimed."""
    sql = "UPDATE raw_docs SET extract_state = 'pending' WHERE extract_state = 'in_progress'"
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
    """Write the extract result onto its raw_docs row (no commit). Compose
    inside a ``write_txn`` with :func:`_upsert_document` so a doc's whole
    materialization lands atomically; :func:`save_extraction` wraps this with a
    commit for the standalone error-recording path."""
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


def _upsert_document(conn, url, site, source_type, content_sha256, doc, now) -> str:
    """Materialize one URL's document row (no commit), deduplicated on the
    extracted-text hash: the corpus keeps at most one row per distinct text --
    the *cleanest* URL of the group (see :func:`_canonical_key`). Compose inside
    a ``write_txn`` alongside :func:`_save_extraction` so a whole doc's writes are
    atomic; :func:`upsert_document` wraps this with a commit for standalone callers.

    Returns "new" / "changed" / "unchanged" as before, plus "duplicate" when this
    URL's text is already represented by a cleaner URL (then no row is written)."""
    h = text_hash(doc["text"])

    # Text-hash dedup. Other present rows carrying this exact text are the same
    # document reached another way -- source 1: many URLs -> one content blob;
    # source 2: byte-different variants -> identical extraction (e.g. TYPO3 cHash
    # permutations). This lookup is global (keyed on text_sha256, not scoped to
    # the current content digest), so cross-digest variants collapse too.
    others = conn.execute(
        "SELECT url FROM documents WHERE text_sha256=? AND url<>? AND present=1",
        (h, url),
    ).fetchall()
    if others:
        cleanest_other = min(others, key=lambda r: _canonical_key(r["url"]))["url"]
        if _canonical_key(cleanest_other) <= _canonical_key(url):
            # A cleaner (or equal) URL already represents this text -> this URL is
            # a duplicate. Retire any stale row it held (it may have been canonical
            # for a since-changed text) and index nothing for it.
            _mark_document_removed(conn, url, now)
            return "duplicate"
        # This URL is cleaner than every current holder -> it wins; retire them.
        for r in others:
            _mark_document_removed(conn, r["url"], now)

    existing = conn.execute("SELECT * FROM documents WHERE url=?", (url,)).fetchone()
    meta = json.dumps(doc.get("metadata")) if doc.get("metadata") else None
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
                doc.get("title"),
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
        return "new"
    if existing["text_sha256"] != h:
        # The extracted TEXT changed -> a genuine content change: bump revision +
        # updated_at so the delta feed re-surfaces it. (Change detection keys on
        # text, not bytes, so cHash byte-churn with identical text stays quiet.)
        conn.execute(
            """UPDATE documents SET content_sha256=?, source_type=?, title=?, text=?,
                   markdown=?, lang=?, word_count=?, metadata=?, text_sha256=?,
                   present=1, revision=revision+1, updated_at=? WHERE url=?""",
            (
                content_sha256,
                source_type,
                doc.get("title"),
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
        return "changed"
    if existing["content_sha256"] != content_sha256:
        # Same text, new raw bytes (cHash churn): refresh the byte pointer
        # silently -- no revision/updated_at bump, so the feed is not spammed.
        conn.execute(
            "UPDATE documents SET content_sha256=?, source_type=? WHERE url=?",
            (content_sha256, source_type, url),
        )
    if existing["present"] == 0:
        # Resurrecting a previously-removed doc: bump updated_at so the delta
        # feed re-surfaces it for re-indexing even though the text is unchanged.
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
    """Retire a document from the live corpus (no commit).

    Guarded on ``present=1`` so this is a true no-op on an already-retired row.
    ``updated_at`` must be stamped only on the live -> retired transition,
    because that stamp *is* the deletion signal :func:`delta` reports
    (``present=0 AND updated_at > since``). Re-stamping it would re-ship the same
    deletion on every subsequent delta -- the hard DELETE this replaced was
    self-limiting, and the tombstone has to be too.
    """
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
    }


def run_dedup(
    conn, batch_size: int = 500, vacuum: bool = True, now: str | None = None
) -> dict:
    """Backfill ``text_sha256`` and retire duplicate documents, keeping the
    single cleanest URL (see :func:`_canonical_key`) per distinct extracted text.

    This is the one-time corpus cleanup *and* an idempotent maintenance pass: a
    second run finds nothing to backfill or delete and performs no writes. It is
    the authoritative backstop for the prevention logic in :func:`_upsert_document`
    (e.g. after a re-crawl re-materializes cHash variants). Do not run it
    concurrently with fetch/extract -- all three write ``documents``.

    Phases:
      A. Backfill ``text_sha256`` where NULL, keyset-paginated by ``id`` in
         ``batch_size`` chunks so only that many texts are resident at once (never
         the whole corpus) in a single O(n) forward pass. This is derived
         metadata -- it never touches ``updated_at`` (no delta spam).
      B. For every present text group with more than one member, keep the cleanest
         URL and retire the rest with a ``present=0`` tombstone (NOT a hard
         DELETE): a loser may already have been handed downstream as an upsert,
         and :func:`delta` can only report a deletion it can still see
         (``present=0 AND updated_at > since``). A deleted row is invisible, so
         the downstream index would keep the orphan forever. Retiring bumps
         ``updated_at`` by design -- that bump *is* the deletion signal.
      C. VACUUM to defragment -- only if anything was retired.

    ``before``/``after`` count the live corpus (``present=1``), so
    ``before - after == deleted`` still holds even though no row is destroyed.
    """
    now = now or time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    before = conn.execute(
        "SELECT COUNT(*) c FROM documents WHERE present=1"
    ).fetchone()["c"]

    backfilled = 0
    last_id = ""
    while True:
        # Keyset-paginate by the PRIMARY KEY `id` (a forward index range scan),
        # NOT `WHERE text_sha256 IS NULL LIMIT n`: that re-scans from the start
        # each batch and, because rows are updated by their scattered hash-id,
        # degrades to O(n^2) over a large table. Advancing `last_id` past every
        # row seen makes this a single O(n) forward pass; skipping already-hashed
        # rows keeps it idempotent and restartable.
        rows = conn.execute(
            "SELECT id, text, text_sha256 FROM documents "
            "WHERE id > ? ORDER BY id LIMIT ?",
            (last_id, batch_size),
        ).fetchall()
        if not rows:
            break
        # Hash outside the write lock (CPU-heavy); write the batch in one txn.
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

    # One ordered pass over the small (id, url, text_sha256) columns -- NOT an
    # N+1 "query each group" loop, which SQLite may service via the present index
    # and rescan the whole table per group (O(n) each). Group the sorted rows in
    # Python; only 3 short columns per row are held, so memory stays tiny even at
    # corpus scale. Deletes go by PK (id), which is always a seek.
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
        # VACUUM cannot run inside a transaction; every write_txn above has
        # already committed, so the connection is in autocommit here. Retiring is
        # now a soft delete, so unlike the old hard DELETE this frees no pages --
        # it only defragments the UPDATE churn, and `deleted` is just the "this
        # pass churned the table" signal. `--no-vacuum` skips it on a large corpus.
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
