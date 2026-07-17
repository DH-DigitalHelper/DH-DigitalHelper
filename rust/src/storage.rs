//! SQLite persistence for Phase 1: schema, content-addressed raw cache, and the
//! low-level write operations composed by the single writer task.
//!
//! The schema is kept byte-for-byte in sync with the Python `storage.py::SCHEMA`
//! (both create `IF NOT EXISTS`, so either side may initialise a fresh DB). The
//! only addition over the historical schema is the `links` edge table.
//!
//! Unlike the old Python path there is no per-claim `work_state='in_progress'`
//! write: the frontier lives in memory (see `writer.rs`), and only terminal
//! `pending -> done/error/removed` transitions are persisted. A crash therefore
//! re-does only the pages that were in flight, which is idempotent.

use std::io;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::{params, Connection};
use sha2::{Digest, Sha256};

pub const SCHEMA: &str = r#"
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
    -- SHA-256 of the normalized extracted text. Written only by the Python
    -- Phase-2/dedup path (storage.py::text_hash); mirrored here so a fresh
    -- Rust-created DB has the identical schema. Nullable: an existing DB gets it
    -- via storage.py::_migrate (ALTER TABLE) and backfilled by `dhbw-scraper dedup`.
    text_sha256      TEXT,
    present          INTEGER NOT NULL DEFAULT 1,
    revision         INTEGER NOT NULL DEFAULT 1,
    first_indexed_at TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_site ON documents(site);
CREATE INDEX IF NOT EXISTS idx_documents_updated ON documents(updated_at);
CREATE INDEX IF NOT EXISTS idx_documents_present ON documents(present);
-- idx_documents_text_sha256 is created by migrate(), not here: on a DB that
-- predates the dedup column, `CREATE TABLE IF NOT EXISTS documents` is a no-op
-- and leaves text_sha256 absent, so a CREATE INDEX in this batch would fail with
-- "no such column" -- and take every statement after it down with it. Mirrors
-- the same reasoning in storage.py::SCHEMA.

-- Outbound link graph: every <a href> a crawled page emits, in-domain and
-- external alike. Following stays in-domain (in_domain=1 marks a follow
-- candidate); external/cross-campus edges are recorded, never crawled.
CREATE TABLE IF NOT EXISTS links (
    src_url       TEXT NOT NULL,
    dst_url       TEXT NOT NULL,
    site          TEXT NOT NULL,
    in_domain     INTEGER NOT NULL,
    depth         INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (src_url, dst_url)
);
CREATE INDEX IF NOT EXISTS idx_links_dst ON links(dst_url);
CREATE INDEX IF NOT EXISTS idx_links_site ON links(site);
"#;

/// A queue row loaded into the in-memory frontier, carrying the stored change-
/// detection validators so a worker can issue a conditional GET.
#[derive(Debug, Clone)]
pub struct FrontierItem {
    pub url: String,
    pub depth: i64,
    pub etag: Option<String>,
    pub last_modified: Option<String>,
    pub content_sha256: Option<String>,
    pub present: bool,
}

/// One outbound edge for the `links` table.
#[derive(Debug, Clone)]
pub struct LinkEdge {
    pub src: String,
    pub dst: String,
    pub site: String,
    pub in_domain: bool,
    pub depth: i64,
    pub first_seen_at: String,
}

/// A present page whose content is a stored HTML blob — the unit of work for an
/// offline `links` backfill (see `backfill.rs`): `(url, site, depth,
/// content_sha256)`.
#[derive(Debug, Clone)]
pub struct BackfillPage {
    pub url: String,
    pub site: String,
    pub depth: i64,
    pub content_sha256: String,
}

/// A discovered followable link to enqueue: `(url, site, depth, discovered_from,
/// first_seen_at)`.
#[derive(Debug, Clone)]
pub struct QueueInsert {
    pub url: String,
    pub site: String,
    pub depth: i64,
    pub discovered_from: String,
    pub first_seen_at: String,
}

pub fn sha256_hex(data: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(data);
    let digest = hasher.finalize();
    let mut s = String::with_capacity(64);
    for b in digest {
        s.push_str(&format!("{b:02x}"));
    }
    s
}

/// UTC timestamp in the `%Y-%m-%dT%H:%M:%S` form the Python side writes.
pub fn now_iso() -> String {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    format_utc(secs)
}

/// Convert a Unix timestamp (seconds) to `YYYY-MM-DDTHH:MM:SS` UTC using Howard
/// Hinnant's days-from-civil algorithm (no external date dependency).
fn format_utc(secs: u64) -> String {
    let days = (secs / 86_400) as i64;
    let rem = secs % 86_400;
    let (hh, mm, ss) = (rem / 3600, (rem % 3600) / 60, rem % 60);
    // days since 1970-01-01 -> civil date
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = doy - (153 * mp + 2) / 5 + 1; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 }; // [1, 12]
    let year = if m <= 2 { y + 1 } else { y };
    format!("{year:04}-{m:02}-{d:02}T{hh:02}:{mm:02}:{ss:02}")
}

pub fn connect(db_file: &str) -> rusqlite::Result<Connection> {
    let conn = Connection::open(db_file)?;
    conn.busy_timeout(std::time::Duration::from_millis(15_000))?;
    conn.execute_batch(
        "PRAGMA journal_mode=WAL;
         PRAGMA synchronous=NORMAL;
         PRAGMA foreign_keys=ON;",
    )?;
    Ok(conn)
}

pub fn init_db(conn: &Connection) -> rusqlite::Result<()> {
    conn.execute_batch(SCHEMA)?;
    migrate(conn)
}

/// Forward-only, idempotent migrations for a DB that predates a column, mirroring
/// `storage.py::_migrate` so either side can open what the other created.
///
/// Column presence is introspected rather than tracked via `user_version`, so this
/// is a no-op whether the column arrived via `SCHEMA` (fresh DB) or an earlier
/// ALTER. `ALTER TABLE ADD COLUMN` with a NULL default is an O(1) metadata-only
/// change in SQLite -- instant even on a multi-GB file.
fn migrate(conn: &Connection) -> rusqlite::Result<()> {
    if !has_column(conn, "documents", "text_sha256")? {
        conn.execute_batch("ALTER TABLE documents ADD COLUMN text_sha256 TEXT")?;
    }
    // Only safe once the column is guaranteed to exist. Composite so the dedup
    // lookup `text_sha256=? AND present=1` is a seek rather than a scan via the
    // low-selectivity present index (see storage.py::_migrate).
    conn.execute_batch(
        "CREATE INDEX IF NOT EXISTS idx_documents_text_sha256
             ON documents(text_sha256, present)",
    )
}

fn has_column(conn: &Connection, table: &str, column: &str) -> rusqlite::Result<bool> {
    let mut stmt = conn.prepare(&format!("PRAGMA table_info({table})"))?;
    let mut rows = stmt.query([])?;
    while let Some(row) = rows.next()? {
        if row.get::<_, String>(1)? == column {
            return Ok(true);
        }
    }
    Ok(false)
}

/// Checkpoint the WAL into the main DB and leave it truncated, so the Python
/// Phase-2 reader opens a fully consistent file. Best-effort.
pub fn checkpoint_truncate(conn: &Connection) {
    let _ = conn.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);");
}

// ---- seeding ---------------------------------------------------------------

/// Enqueue a URL (seed), leaving an existing row untouched (INSERT OR IGNORE).
pub fn enqueue(
    conn: &Connection,
    url: &str,
    site: &str,
    depth: i64,
    discovered_from: Option<&str>,
    now: &str,
) -> rusqlite::Result<bool> {
    let n = conn.execute(
        "INSERT OR IGNORE INTO queue (url, site, depth, discovered_from, first_seen_at)
         VALUES (?, ?, ?, ?, ?)",
        params![url, site, depth, discovered_from, now],
    )?;
    Ok(n > 0)
}

/// Record a sitemap `<lastmod>` for `url`, re-queuing it only if the value
/// strictly advanced. Faithful port of Python `set_sitemap_lastmod`.
pub fn set_sitemap_lastmod(
    conn: &Connection,
    url: &str,
    site: &str,
    lastmod: Option<&str>,
    now: &str,
) -> rusqlite::Result<()> {
    let stored: Option<Option<String>> = conn
        .query_row(
            "SELECT sitemap_lastmod FROM queue WHERE url = ?",
            [url],
            |r| r.get::<_, Option<String>>(0),
        )
        .ok();
    match stored {
        None => {
            conn.execute(
                "INSERT INTO queue (url, site, sitemap_lastmod, first_seen_at)
                 VALUES (?, ?, ?, ?)",
                params![url, site, lastmod, now],
            )?;
        }
        Some(stored) => match (lastmod, stored.as_deref()) {
            (Some(lm), Some(st)) if lm > st => {
                conn.execute(
                    "UPDATE queue SET sitemap_lastmod = ?, work_state = 'pending' WHERE url = ?",
                    params![lm, url],
                )?;
            }
            (Some(lm), None) => {
                conn.execute(
                    "UPDATE queue SET sitemap_lastmod = ? WHERE url = ?",
                    params![lm, url],
                )?;
            }
            _ => {}
        },
    }
    Ok(())
}

/// Reset present, already-fetched URLs for a site back to 'pending' (recheck=all).
pub fn requeue_present_urls(conn: &Connection, site: &str) -> rusqlite::Result<usize> {
    conn.execute(
        "UPDATE queue SET work_state = 'pending'
         WHERE site = ? AND present = 1 AND work_state = 'done'",
        [site],
    )
}

/// Recover rows stranded 'in_progress' by a crashed prior run.
pub fn reset_in_progress(conn: &Connection) -> rusqlite::Result<usize> {
    conn.execute(
        "UPDATE queue SET work_state = 'pending' WHERE work_state = 'in_progress'",
        [],
    )
}

/// Load the frontier for a site: all 'pending' rows (optionally only those never
/// fetched, for recheck=new-only), ordered depth then url.
pub fn load_pending(
    conn: &Connection,
    site: &str,
    only_new: bool,
) -> rusqlite::Result<Vec<FrontierItem>> {
    let sql = if only_new {
        "SELECT url, depth, etag, last_modified, content_sha256, present
         FROM queue WHERE site = ? AND work_state = 'pending' AND last_checked_at IS NULL
         ORDER BY depth, url"
    } else {
        "SELECT url, depth, etag, last_modified, content_sha256, present
         FROM queue WHERE site = ? AND work_state = 'pending'
         ORDER BY depth, url"
    };
    let mut stmt = conn.prepare(sql)?;
    let rows = stmt.query_map([site], |r| {
        Ok(FrontierItem {
            url: r.get(0)?,
            depth: r.get(1)?,
            etag: r.get(2)?,
            last_modified: r.get(3)?,
            content_sha256: r.get(4)?,
            present: r.get::<_, i64>(5)? != 0,
        })
    })?;
    rows.collect()
}

/// Every present page whose stored content is an HTML blob — the input to an
/// offline `links` backfill. The JOIN to `raw_docs` keeps only URLs whose
/// `content_sha256` resolves to a `source_type='html'` blob, so PDFs (which never
/// emit edges) and pages that were never fetched are excluded. Ordered so the
/// pass is deterministic and touches one site at a time.
pub fn load_backfill_pages(conn: &Connection) -> rusqlite::Result<Vec<BackfillPage>> {
    let mut stmt = conn.prepare(
        "SELECT q.url, q.site, q.depth, q.content_sha256
         FROM queue q JOIN raw_docs r ON q.content_sha256 = r.content_sha256
         WHERE q.present = 1 AND q.content_sha256 IS NOT NULL AND r.source_type = 'html'
         ORDER BY q.site, q.depth, q.url",
    )?;
    let rows = stmt.query_map([], |r| {
        Ok(BackfillPage {
            url: r.get(0)?,
            site: r.get(1)?,
            depth: r.get(2)?,
            content_sha256: r.get(3)?,
        })
    })?;
    rows.collect()
}

/// All known URLs for a site (any state) — seeds the in-memory dedup set so the
/// heap never re-adds a URL that already exists, mirroring INSERT OR IGNORE.
pub fn all_urls(conn: &Connection, site: &str) -> rusqlite::Result<Vec<String>> {
    let mut stmt = conn.prepare("SELECT url FROM queue WHERE site = ?")?;
    let rows = stmt.query_map([site], |r| r.get::<_, String>(0))?;
    rows.collect()
}

// ---- per-page terminal writes (composed inside one transaction) ------------

#[allow(clippy::too_many_arguments)]
pub fn mark_url_checked(
    conn: &Connection,
    url: &str,
    http_status: i64,
    etag: Option<&str>,
    last_modified: Option<&str>,
    content_sha256: Option<&str>,
    changed: bool,
    present: bool,
    now: &str,
) -> rusqlite::Result<()> {
    if changed {
        conn.execute(
            "UPDATE queue SET work_state='done', http_status=?, etag=?,
                 last_modified=?, content_sha256=?, present=?,
                 last_checked_at=?, last_changed_at=? WHERE url=?",
            params![
                http_status,
                etag,
                last_modified,
                content_sha256,
                present as i64,
                now,
                now,
                url
            ],
        )?;
    } else {
        // COALESCE, not a plain assignment: an unchanged response that simply
        // omitted its validators must not erase the ones we already hold. These
        // sites emit ETag/Last-Modified erratically, and dropping a stored
        // validator costs a full body on every subsequent crawl. A response that
        // *does* carry a validator still refreshes it.
        conn.execute(
            "UPDATE queue SET work_state='done', http_status=?,
                 etag=COALESCE(?, etag), last_modified=COALESCE(?, last_modified),
                 present=?, last_checked_at=? WHERE url=?",
            params![http_status, etag, last_modified, present as i64, now, url],
        )?;
    }
    Ok(())
}

pub fn mark_url_error(
    conn: &Connection,
    url: &str,
    http_status: Option<i64>,
    now: &str,
) -> rusqlite::Result<()> {
    conn.execute(
        "UPDATE queue SET work_state='error', http_status=?, last_checked_at=? WHERE url=?",
        params![http_status, now, url],
    )?;
    Ok(())
}

pub fn mark_url_removed(
    conn: &Connection,
    url: &str,
    http_status: i64,
    now: &str,
) -> rusqlite::Result<()> {
    // Clearing the validators is load-bearing, not tidiness. Keeping them let a
    // removed page that a sitemap <lastmod> advance re-pended still send
    // If-None-Match and get a 304 -- and the 304 branch marks present:true while
    // touching neither documents nor raw_docs, so the row ended up present=1
    // against documents.present=0: "present" yet missing from the corpus, and
    // never re-materialized. With no validator the next fetch must be a full GET,
    // which lands on the 2xx path where `!present` makes content_outcome report
    // `changed`, re-emitting the raw-doc hand-off and resurrecting the document.
    conn.execute(
        "UPDATE queue SET work_state='done', present=0, http_status=?,
             etag=NULL, last_modified=NULL,
             last_checked_at=?, last_changed_at=? WHERE url=?",
        params![http_status, now, now, url],
    )?;
    Ok(())
}

pub fn mark_document_removed(conn: &Connection, url: &str, now: &str) -> rusqlite::Result<()> {
    conn.execute(
        "UPDATE documents SET present=0, updated_at=? WHERE url=?",
        params![now, url],
    )?;
    Ok(())
}

/// INSERT OR IGNORE many followable links. Returns rows newly inserted.
pub fn enqueue_many(conn: &Connection, rows: &[QueueInsert]) -> rusqlite::Result<usize> {
    let mut stmt = conn.prepare(
        "INSERT OR IGNORE INTO queue (url, site, depth, discovered_from, first_seen_at)
         VALUES (?, ?, ?, ?, ?)",
    )?;
    let mut inserted = 0;
    for r in rows {
        inserted += stmt.execute(params![
            r.url,
            r.site,
            r.depth,
            r.discovered_from,
            r.first_seen_at
        ])?;
    }
    Ok(inserted)
}

/// INSERT OR IGNORE the full outbound edge set for one page.
pub fn insert_links(conn: &Connection, edges: &[LinkEdge]) -> rusqlite::Result<()> {
    let mut stmt = conn.prepare(
        "INSERT OR IGNORE INTO links (src_url, dst_url, site, in_domain, depth, first_seen_at)
         VALUES (?, ?, ?, ?, ?, ?)",
    )?;
    for e in edges {
        stmt.execute(params![
            e.src,
            e.dst,
            e.site,
            e.in_domain as i64,
            e.depth,
            e.first_seen_at
        ])?;
    }
    Ok(())
}

/// Insert a raw_docs row if absent (race-safe). Returns true iff newly inserted
/// (caller requeues extraction otherwise).
pub fn upsert_raw_doc(
    conn: &Connection,
    content_sha256: &str,
    source_type: &str,
    raw_path: &str,
    bytes: i64,
    now: &str,
) -> rusqlite::Result<bool> {
    let n = conn.execute(
        "INSERT INTO raw_docs (content_sha256, source_type, raw_path, bytes,
             first_seen_at, extract_state)
         VALUES (?, ?, ?, ?, ?, 'pending')
         ON CONFLICT(content_sha256) DO NOTHING",
        params![content_sha256, source_type, raw_path, bytes, now],
    )?;
    Ok(n == 1)
}

pub fn requeue_extraction(conn: &Connection, content_sha256: &str) -> rusqlite::Result<bool> {
    let n = conn.execute(
        "UPDATE raw_docs SET extract_state='pending' WHERE content_sha256=?",
        [content_sha256],
    )?;
    Ok(n > 0)
}

#[allow(clippy::too_many_arguments)]
pub fn record_fetch(
    conn: &Connection,
    run_id: &str,
    url: &str,
    final_url: &str,
    site: &str,
    status: Option<i64>,
    content_type: Option<&str>,
    sha256: Option<&str>,
    bytes: i64,
    kind: Option<&str>,
    outcome: &str,
    error: Option<&str>,
    now: &str,
) -> rusqlite::Result<()> {
    conn.execute(
        "INSERT INTO crawl_log
           (run_id, url, final_url, site, status, content_type, sha256, bytes,
            kind, outcome, error, fetched_at)
         VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        params![
            run_id,
            url,
            final_url,
            site,
            status,
            content_type,
            sha256,
            bytes,
            kind,
            outcome,
            error,
            now
        ],
    )?;
    Ok(())
}

// ---- content-addressed raw cache -------------------------------------------

static TMP_COUNTER: AtomicU64 = AtomicU64::new(0);

/// Content-addressed store for downloaded bytes under `root`.
#[derive(Clone)]
pub struct RawCache {
    root: PathBuf,
}

impl RawCache {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }

    pub fn path_for(&self, digest: &str, ext: &str) -> PathBuf {
        let ext = if !ext.is_empty() && !ext.starts_with('.') {
            format!(".{ext}")
        } else {
            ext.to_string()
        };
        self.root.join(format!("{digest}{ext}"))
    }

    /// Write `data` at its content-addressed path (idempotent). Uses a unique
    /// temp file + rename so a concurrent Phase-2 reader never sees a partial
    /// file. Returns `(digest, path)`.
    pub fn write(&self, data: &[u8], ext: &str) -> io::Result<(String, PathBuf)> {
        let digest = sha256_hex(data);
        let path = self.path_for(&digest, ext);
        if path.is_file() {
            return Ok((digest, path));
        }
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let n = TMP_COUNTER.fetch_add(1, Ordering::Relaxed);
        let tmp = path.with_file_name(format!(
            "{}.{}.{}.tmp",
            path.file_name().and_then(|s| s.to_str()).unwrap_or("blob"),
            std::process::id(),
            n
        ));
        std::fs::write(&tmp, data)?;
        match std::fs::rename(&tmp, &path) {
            Ok(()) => Ok((digest, path)),
            Err(_) if path.is_file() => {
                // Another worker won the race with identical bytes.
                let _ = std::fs::remove_file(&tmp);
                Ok((digest, path))
            }
            Err(e) => {
                let _ = std::fs::remove_file(&tmp);
                Err(e)
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn format_utc_matches_known_epochs() {
        assert_eq!(format_utc(0), "1970-01-01T00:00:00");
        // 2026-07-16T00:00:00Z = 1_784_160_000
        assert_eq!(format_utc(1_784_160_000), "2026-07-16T00:00:00");
    }

    #[test]
    fn sha256_hex_is_lowercase_hex() {
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn documents_schema_has_text_sha256_column() {
        // Keeps the Rust SCHEMA in parity with Python's dedup column so a
        // fresh Rust-created DB and a Python-migrated one have identical shape.
        let conn = Connection::open_in_memory().unwrap();
        init_db(&conn).unwrap();
        let cols: Vec<String> = conn
            .prepare("PRAGMA table_info(documents)")
            .unwrap()
            .query_map([], |row| row.get::<_, String>(1))
            .unwrap()
            .map(|r| r.unwrap())
            .collect();
        assert!(
            cols.iter().any(|c| c == "text_sha256"),
            "documents is missing text_sha256; columns = {cols:?}"
        );
    }

    fn seed_checked_row(conn: &Connection) {
        init_db(conn).unwrap();
        conn.execute(
            "INSERT INTO queue (url, site, work_state, etag, last_modified,
                 content_sha256, present, first_seen_at)
             VALUES ('https://x.de/p', 'x.de', 'pending', '\"v1\"',
                 'Mon, 01 Jan 2026 00:00:00 GMT', 'aaa', 1, '2026-07-16T00:00:00')",
            [],
        )
        .unwrap();
    }

    fn validators(conn: &Connection) -> (Option<String>, Option<String>) {
        conn.query_row(
            "SELECT etag, last_modified FROM queue WHERE url = 'https://x.de/p'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap()
    }

    /// These sites serve flaky validators: the same page answers 200 with an ETag
    /// one run and without one the next. If the validator-less response erases the
    /// stored ETag, the next crawl can no longer send If-None-Match and re-downloads
    /// the full body every single run — which defeats the whole incremental design.
    #[test]
    fn unchanged_check_without_validators_preserves_stored_ones() {
        let conn = Connection::open_in_memory().unwrap();
        seed_checked_row(&conn);

        mark_url_checked(
            &conn,
            "https://x.de/p",
            200,
            None, // response carried no ETag ...
            None, // ... and no Last-Modified
            Some("aaa"),
            false, // unchanged
            true,
            "2026-07-17T00:00:00",
        )
        .unwrap();

        let (etag, lm) = validators(&conn);
        assert_eq!(etag.as_deref(), Some("\"v1\""), "stored ETag must survive");
        assert_eq!(
            lm.as_deref(),
            Some("Mon, 01 Jan 2026 00:00:00 GMT"),
            "stored Last-Modified must survive"
        );
    }

    /// A removed page must not be able to 304 its way back to present=1.
    ///
    /// mark_url_removed kept the validators, so a removed page re-pended by a
    /// sitemap <lastmod> advance could still send If-None-Match and get a 304 --
    /// and the 304 branch hardcodes present:true while touching neither documents
    /// nor raw_docs. The result was queue.present=1 against documents.present=0:
    /// "present" yet absent from the corpus, and never re-materialized, because
    /// the resurrection logic only lives on the full-body 2xx path.
    #[test]
    fn mark_url_removed_clears_validators_so_the_page_cannot_304() {
        let conn = Connection::open_in_memory().unwrap();
        seed_checked_row(&conn);

        mark_url_removed(&conn, "https://x.de/p", 404, "2026-07-17T00:00:00").unwrap();

        let (etag, lm) = validators(&conn);
        assert_eq!(
            etag, None,
            "a removed page must not keep a conditional-GET validator"
        );
        assert_eq!(lm, None);
        let present: i64 = conn
            .query_row(
                "SELECT present FROM queue WHERE url = 'https://x.de/p'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(present, 0, "it is still marked removed");
    }

    /// A DB created before `text_sha256` existed must still open. `CREATE TABLE IF
    /// NOT EXISTS` is a no-op on an existing table, so the column never appears by
    /// itself -- Python survives this via `_migrate`'s ALTER, and Rust has to match
    /// or Phase 1 dies on startup against a DB Phase 2 opens happily.
    #[test]
    fn init_db_migrates_a_pre_text_sha256_documents_table() {
        let conn = Connection::open_in_memory().unwrap();
        // The `documents` table exactly as it looked before the dedup column.
        conn.execute_batch(
            "CREATE TABLE documents (
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
             );",
        )
        .unwrap();

        init_db(&conn).expect("init_db must migrate an old DB, not fail on it");

        let cols: Vec<String> = conn
            .prepare("PRAGMA table_info(documents)")
            .unwrap()
            .query_map([], |row| row.get::<_, String>(1))
            .unwrap()
            .map(|r| r.unwrap())
            .collect();
        assert!(
            cols.iter().any(|c| c == "text_sha256"),
            "migrate must add text_sha256; columns = {cols:?}"
        );
        // And it stays idempotent on a second open.
        init_db(&conn).expect("init_db must be idempotent");
    }

    /// The flip side: a response that *does* carry validators still refreshes them,
    /// so preserving-on-None must not turn into never-updating.
    #[test]
    fn unchanged_check_with_validators_refreshes_them() {
        let conn = Connection::open_in_memory().unwrap();
        seed_checked_row(&conn);

        mark_url_checked(
            &conn,
            "https://x.de/p",
            200,
            Some("\"v2\""),
            Some("Tue, 02 Jan 2026 00:00:00 GMT"),
            Some("aaa"),
            false,
            true,
            "2026-07-17T00:00:00",
        )
        .unwrap();

        let (etag, lm) = validators(&conn);
        assert_eq!(etag.as_deref(), Some("\"v2\""));
        assert_eq!(lm.as_deref(), Some("Tue, 02 Jan 2026 00:00:00 GMT"));
    }
}
