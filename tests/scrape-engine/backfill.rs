//! Offline `links` backfill: rebuild the edge table from raw HTML on disk with no
//! crawl. Seeds the DB + content-addressed cache directly (the state a real crawl
//! leaves behind) and drives `backfill::run` against it.

use _engine::backfill;
use _engine::config::{RunConfig, SiteCfg};
use _engine::progress::ProgressSink;
use _engine::storage::{self, RawCache};

fn config(dir: &std::path::Path) -> RunConfig {
    RunConfig {
        sites: vec![SiteCfg {
            name: "t".into(),
            seed_url: "http://site.test/".into(),
            allowed_domain: "site.test".into(),
        }],
        use_sitemap: false,
        max_pages: 0,
        max_pages_per_host: 0,
        request_delay_seconds: 0.0,
        workers_per_host: 1,
        recheck: "all".into(),
        user_agent: "test".into(),
        db_file: dir.join("db.sqlite3").to_string_lossy().into_owned(),
        raw_dir: dir.join("raw").to_string_lossy().into_owned(),
    }
}

/// Seed one present HTML page as a real crawl would: write its raw blob to the
/// content-addressed cache and insert the matching `queue` + `raw_docs` rows.
fn seed_page(
    conn: &rusqlite::Connection,
    cache: &RawCache,
    url: &str,
    site: &str,
    depth: i64,
    html: &str,
) {
    let (sha, path) = cache.write(html.as_bytes(), ".html").unwrap();
    conn.execute(
        "INSERT INTO queue (url, site, depth, content_sha256, present, work_state, first_seen_at)
         VALUES (?, ?, ?, ?, 1, 'done', '2026-07-16T00:00:00')",
        rusqlite::params![url, site, depth, sha],
    )
    .unwrap();
    storage::upsert_raw_doc(
        conn,
        &sha,
        "html",
        &path.to_string_lossy(),
        html.len() as i64,
        "2026-07-16T00:00:00",
    )
    .unwrap();
}

#[test]
fn backfills_edges_from_raw_html_offline() {
    let tmp = tempfile::tempdir().unwrap();
    let cfg = config(tmp.path());
    let cache = RawCache::new(&cfg.raw_dir);
    {
        let conn = storage::connect(&cfg.db_file).unwrap();
        storage::init_db(&conn).unwrap();
        seed_page(
            &conn,
            &cache,
            "http://site.test/p",
            "site.test",
            0,
            r#"<html><body>
               <a href="/a">a</a>
               <a href="/a">dup a</a>
               <a href="http://external.test/x">ext</a>
               <a href="mailto:me@x.test">mail</a>
            </body></html>"#,
        );
    }

    let counts = backfill::run(cfg.clone(), ProgressSink::new(None)).unwrap();
    assert_eq!(counts.pages, 1);
    assert_eq!(counts.raw_missing, 0);
    // /a (deduped to one) + external = 2 edges; mailto is dropped.
    assert_eq!(counts.edges, 2);

    let conn = rusqlite::Connection::open(&cfg.db_file).unwrap();
    let total: i64 = conn
        .query_row("SELECT COUNT(*) FROM links", [], |r| r.get(0))
        .unwrap();
    assert_eq!(total, 2);

    // In-domain edge flagged for following; external recorded but in_domain=0.
    let indomain: i64 = conn
        .query_row(
            "SELECT in_domain FROM links WHERE src_url=? AND dst_url=?",
            ["http://site.test/p", "http://site.test/a"],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(indomain, 1);
    let ext: i64 = conn
        .query_row(
            "SELECT in_domain FROM links WHERE dst_url=?",
            ["http://external.test/x"],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(ext, 0);

    // Edge depth mirrors the live path: page depth + 1.
    let depth: i64 = conn
        .query_row(
            "SELECT depth FROM links WHERE dst_url=?",
            ["http://site.test/a"],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(depth, 1);
}

#[test]
fn backfill_is_idempotent() {
    let tmp = tempfile::tempdir().unwrap();
    let cfg = config(tmp.path());
    let cache = RawCache::new(&cfg.raw_dir);
    {
        let conn = storage::connect(&cfg.db_file).unwrap();
        storage::init_db(&conn).unwrap();
        seed_page(
            &conn,
            &cache,
            "http://site.test/p",
            "site.test",
            0,
            r#"<a href="/a">a</a><a href="/b">b</a>"#,
        );
    }

    let first = backfill::run(cfg.clone(), ProgressSink::new(None)).unwrap();
    assert_eq!(first.edges, 2);

    // Re-run: same pages re-parsed, but INSERT OR IGNORE adds no new edges.
    let second = backfill::run(cfg.clone(), ProgressSink::new(None)).unwrap();
    assert_eq!(second.pages, 1);
    assert_eq!(second.edges, 0, "re-run inserts no new edges");

    let conn = rusqlite::Connection::open(&cfg.db_file).unwrap();
    let total: i64 = conn
        .query_row("SELECT COUNT(*) FROM links", [], |r| r.get(0))
        .unwrap();
    assert_eq!(total, 2, "edge count stable across re-runs");
}

#[test]
fn missing_raw_blob_is_counted_not_fatal() {
    let tmp = tempfile::tempdir().unwrap();
    let cfg = config(tmp.path());
    {
        let conn = storage::connect(&cfg.db_file).unwrap();
        storage::init_db(&conn).unwrap();
        // queue + raw_docs rows that reference a sha whose blob was never written.
        conn.execute(
            "INSERT INTO queue (url, site, depth, content_sha256, present, work_state, first_seen_at)
             VALUES ('http://site.test/gone', 'site.test', 0, 'deadbeef', 1, 'done', '2026-07-16T00:00:00')",
            [],
        )
        .unwrap();
        storage::upsert_raw_doc(
            &conn,
            "deadbeef",
            "html",
            "/nope/deadbeef.html",
            0,
            "2026-07-16T00:00:00",
        )
        .unwrap();
    }

    let counts = backfill::run(cfg, ProgressSink::new(None)).unwrap();
    assert_eq!(counts.raw_missing, 1);
    assert_eq!(counts.pages, 0);
    assert_eq!(counts.edges, 0);
}

#[test]
fn pdf_pages_are_skipped() {
    let tmp = tempfile::tempdir().unwrap();
    let cfg = config(tmp.path());
    let cache = RawCache::new(&cfg.raw_dir);
    {
        let conn = storage::connect(&cfg.db_file).unwrap();
        storage::init_db(&conn).unwrap();
        // A PDF page: blob on disk, raw_docs source_type='pdf'. Even though its bytes
        // happen to contain an <a href>, backfill must not treat it as HTML.
        let pdf_bytes = br#"%PDF-1.4 <a href="/leak">x</a>"#;
        let (sha, path) = cache.write(pdf_bytes, ".pdf").unwrap();
        conn.execute(
            "INSERT INTO queue (url, site, depth, content_sha256, present, work_state, first_seen_at)
             VALUES ('http://site.test/doc.pdf', 'site.test', 0, ?, 1, 'done', '2026-07-16T00:00:00')",
            rusqlite::params![sha],
        )
        .unwrap();
        storage::upsert_raw_doc(
            &conn,
            &sha,
            "pdf",
            &path.to_string_lossy(),
            pdf_bytes.len() as i64,
            "2026-07-16T00:00:00",
        )
        .unwrap();
    }

    let counts = backfill::run(cfg.clone(), ProgressSink::new(None)).unwrap();
    assert_eq!(counts.pages, 0, "pdf page not read as html");
    assert_eq!(counts.edges, 0);

    let conn = rusqlite::Connection::open(&cfg.db_file).unwrap();
    let total: i64 = conn
        .query_row("SELECT COUNT(*) FROM links", [], |r| r.get(0))
        .unwrap();
    assert_eq!(total, 0, "no edges from a pdf blob");
}

#[test]
fn absent_pages_are_not_backfilled() {
    let tmp = tempfile::tempdir().unwrap();
    let cfg = config(tmp.path());
    let cache = RawCache::new(&cfg.raw_dir);
    {
        let conn = storage::connect(&cfg.db_file).unwrap();
        storage::init_db(&conn).unwrap();
        // A removed page (present=0) still has a stored blob, but the live path emits
        // no edges for removed pages, so backfill must skip it too.
        let html = r#"<a href="/a">a</a>"#;
        let (sha, path) = cache.write(html.as_bytes(), ".html").unwrap();
        conn.execute(
            "INSERT INTO queue (url, site, depth, content_sha256, present, work_state, first_seen_at)
             VALUES ('http://site.test/gone', 'site.test', 0, ?, 0, 'done', '2026-07-16T00:00:00')",
            rusqlite::params![sha],
        )
        .unwrap();
        storage::upsert_raw_doc(
            &conn,
            &sha,
            "html",
            &path.to_string_lossy(),
            html.len() as i64,
            "2026-07-16T00:00:00",
        )
        .unwrap();
    }

    let counts = backfill::run(cfg, ProgressSink::new(None)).unwrap();
    assert_eq!(counts.pages, 0, "present=0 page skipped");
    assert_eq!(counts.edges, 0);
}
