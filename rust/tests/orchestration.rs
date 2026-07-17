//! End-to-end orchestration tests using an in-memory `HttpClient` (the Rust
//! analogue of the Python `fetch_fn` injection in `test_crawl.py`). Drives a
//! tiny fixture site through the real frontier + single-writer pipeline and
//! asserts the resulting SQLite database.

use std::collections::HashMap;
use std::sync::Arc;

use _native::config::{RunConfig, SiteCfg};
use _native::crawl::run_with_client;
use _native::fetch::{FetchRequest, FetchResult, HttpClient};
use _native::progress::ProgressSink;

/// One canned response. `Default` is a 200 with an empty body, so a test spells
/// out only the field it cares about.
#[derive(Clone, Default)]
struct Page {
    content_type: String,
    body: Vec<u8>,
    /// 0 means 200.
    status: u16,
    /// Set to model a redirect: reqwest follows it and reports where the bytes
    /// actually came from, which may be a different host than was requested.
    final_url: Option<String>,
}

impl Page {
    fn html(body: &str) -> Self {
        Self {
            content_type: "text/html".into(),
            body: body.as_bytes().to_vec(),
            ..Default::default()
        }
    }

    /// The request was redirected, and the body below came from `final_url`.
    fn redirected_to(mut self, final_url: &str) -> Self {
        self.final_url = Some(final_url.into());
        self
    }
}

#[derive(Clone)]
struct MockClient {
    pages: Arc<HashMap<String, Page>>,
}

impl MockClient {
    /// Shorthand for the common case: `(url, content_type, body)` -> a plain 200.
    fn new(pages: Vec<(&str, &str, &str)>) -> Self {
        Self::from_pages(
            pages
                .into_iter()
                .map(|(url, ct, body)| {
                    (
                        url,
                        Page {
                            content_type: ct.to_string(),
                            body: body.as_bytes().to_vec(),
                            ..Default::default()
                        },
                    )
                })
                .collect(),
        )
    }

    fn from_pages(pages: Vec<(&str, Page)>) -> Self {
        Self {
            pages: Arc::new(pages.into_iter().map(|(u, p)| (u.to_string(), p)).collect()),
        }
    }
}

impl HttpClient for MockClient {
    async fn fetch(&self, req: FetchRequest, _ua: String) -> FetchResult {
        let Some(page) = self.pages.get(&req.url) else {
            return FetchResult {
                url: req.url.clone(),
                final_url: req.url,
                status: 404,
                content_type: String::new(),
                data: Vec::new(),
                etag: None,
                last_modified: None,
                error: Some("HTTP 404".into()),
            };
        };
        let status = if page.status == 0 { 200 } else { page.status };
        // Mirrors ReqwestClient: a non-2xx still yields an error string, a 2xx does not.
        let error = if (200..300).contains(&status) {
            None
        } else {
            Some(format!("HTTP {status}"))
        };
        FetchResult {
            final_url: page.final_url.clone().unwrap_or_else(|| req.url.clone()),
            url: req.url,
            status,
            content_type: page.content_type.clone(),
            data: page.body.clone(),
            etag: None,
            last_modified: None,
            error,
        }
    }

    async fn fetch_bytes(&self, url: String, _ua: String) -> Option<Vec<u8>> {
        self.pages.get(&url).map(|p| p.body.clone())
    }
}

fn config(dir: &std::path::Path) -> RunConfig {
    RunConfig {
        sites: vec![SiteCfg {
            name: "t".into(),
            seed_url: "http://site.test/startseite".into(),
            allowed_domain: "site.test".into(),
        }],
        use_sitemap: true,
        max_pages: 0,
        max_pages_per_host: 0,
        request_delay_seconds: 0.0,
        workers_per_host: 4,
        recheck: "all".into(),
        user_agent: "test".into(),
        db_file: dir.join("db.sqlite3").to_string_lossy().into_owned(),
        raw_dir: dir.join("raw").to_string_lossy().into_owned(),
    }
}

fn fixture() -> MockClient {
    MockClient::new(vec![
        (
            "http://site.test/startseite",
            "text/html",
            r#"<html><body>seed
              <a href="/a">a</a>
              <a href="/b">b</a>
              <a href="/a">dup a</a>
              <a href="/calendar/view.php?view=month&time=1">trap</a>
              <a href="http://external.test/x">external</a>
            </body></html>"#,
        ),
        (
            "http://site.test/a",
            "text/html",
            r#"<html><body>page a <a href="/b">b</a><a href="/c">c</a></body></html>"#,
        ),
        (
            "http://site.test/b",
            "text/html",
            r#"<html><body>page b <a href="/a">a</a></body></html>"#,
        ),
        (
            "http://site.test/c",
            "text/html",
            r#"<html><body>page c leaf</body></html>"#,
        ),
        (
            "http://site.test/from-sitemap",
            "text/html",
            r#"<html><body>sitemap page leaf</body></html>"#,
        ),
        (
            "http://site.test/sitemap.xml",
            "application/xml",
            r#"<urlset>
              <url><loc>http://site.test/from-sitemap</loc></url>
              <url><loc>http://site.test/startseite</loc></url>
            </urlset>"#,
        ),
    ])
}

fn run(dir: &std::path::Path) -> HashMap<String, _native::writer::Counts> {
    run_with_client(
        config(dir),
        "run-test".into(),
        false,
        ProgressSink::new(None),
        fixture(),
    )
    .expect("crawl run")
}

#[test]
fn crawls_cascade_records_edges_and_seeds_from_sitemap() {
    let tmp = tempfile::tempdir().unwrap();
    let counts = run(tmp.path());

    let c = &counts["site.test"];
    // startseite, a, b, c, from-sitemap = 5 distinct pages, all brand new.
    assert_eq!(c.fetched, 5, "fetched count");
    assert_eq!(c.new, 5, "all new on a cold crawl");
    assert_eq!(c.error, 0);

    let conn = rusqlite::Connection::open(tmp.path().join("db.sqlite3")).unwrap();

    // Queue: exactly the 5 followable pages, all done+present; trap & external
    // are NOT enqueued.
    let done: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM queue WHERE work_state='done' AND present=1",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(done, 5);
    for absent in [
        "http://external.test/x",
        "http://site.test/calendar/view.php?view=month&time=1",
    ] {
        let n: i64 = conn
            .query_row("SELECT COUNT(*) FROM queue WHERE url=?", [absent], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(n, 0, "{absent} must not be enqueued");
    }

    // Links edge table: the full outbound set from the seed, incl. external
    // (in_domain=0) and the trap (recorded but never followed).
    let ext: i64 = conn
        .query_row(
            "SELECT in_domain FROM links WHERE src_url=? AND dst_url=?",
            ["http://site.test/startseite", "http://external.test/x"],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(ext, 0, "external edge recorded with in_domain=0");
    let trap: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM links WHERE dst_url=?",
            ["http://site.test/calendar/view.php?view=month&time=1"],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(trap, 1, "trap edge recorded even though never crawled");

    // raw_docs handed off for extraction (one per unique body).
    let raw_pending: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM raw_docs WHERE extract_state='pending'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(raw_pending, 5);

    // Content-addressed raw files exist on disk.
    let files = std::fs::read_dir(tmp.path().join("raw")).unwrap().count();
    assert_eq!(files, 5);
}

/// A raw-cache write failure must never leave a page claiming a stored digest it
/// has no bytes for: `queue.content_sha256` is the change-detection key, so
/// advancing it without a `raw_docs` row makes the page read as Unchanged forever
/// while Phase 2 never sees it — a silent, permanent hole in the corpus.
#[test]
fn raw_cache_write_failure_never_orphans_a_page() {
    let tmp = tempfile::tempdir().unwrap();
    // Put a regular file exactly where RawCache must create its directory, so
    // every `create_dir_all` inside RawCache::write fails deterministically.
    std::fs::write(tmp.path().join("raw"), b"not a directory").unwrap();

    let counts = run_with_client(
        config(tmp.path()),
        "run-rawfail".into(),
        false,
        ProgressSink::new(None),
        fixture(),
    )
    .expect("a raw-write failure must not abort the whole crawl");

    let conn = rusqlite::Connection::open(tmp.path().join("db.sqlite3")).unwrap();
    let orphans: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM queue q
             WHERE q.present = 1 AND q.content_sha256 IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM raw_docs r WHERE r.content_sha256 = q.content_sha256
               )",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(
        orphans, 0,
        "a page whose bytes were never cached must not advertise a stored digest"
    );

    // And the failure must be loud rather than counted as a successful fetch.
    let c = &counts["site.test"];
    assert_eq!(c.new, 0, "nothing was stored, so nothing is 'new'");
    assert!(c.error > 0, "raw-write failures must surface as errors");

    // Losing the bytes must not also lose the link discovery that already
    // succeeded: the body WAS downloaded and parsed, only the cache write
    // failed. Dropping the outbound links would amputate the whole subtree
    // behind a page over one transient disk hiccup, so the cascade must still
    // reach a/b/c (5 pages) rather than stopping at the two seeded ones.
    assert_eq!(
        c.fetched, 5,
        "discovered links must still be followed after a raw-write failure"
    );
    let edges: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM links WHERE src_url = 'http://site.test/startseite'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(
        edges > 0,
        "edges discovered from the fetched body must survive"
    );
}

/// The domain allowlist is enforced on discovered links and at enqueue, but
/// reqwest follows redirects to ANY host. So an in-domain URL that 30x's to a
/// foreign host had that host's bytes downloaded, hashed, cached and link-scanned,
/// all attributed to the in-domain URL -- the allowlist silently bypassed.
#[test]
fn off_domain_redirect_content_is_not_stored() {
    let tmp = tempfile::tempdir().unwrap();
    let client = MockClient::from_pages(vec![
        (
            "http://site.test/startseite",
            Page::html(r#"<html><body>seed <a href="/go">go</a></body></html>"#),
        ),
        // Requested in-domain, but the bytes actually came from another host.
        (
            "http://site.test/go",
            Page::html("<html><body>foreign host content, not ours at all</body></html>")
                .redirected_to("http://evil.test/landing"),
        ),
    ]);
    let mut cfg = config(tmp.path());
    cfg.use_sitemap = false;

    let counts = run_with_client(
        cfg,
        "run-redir".into(),
        false,
        ProgressSink::new(None),
        client,
    )
    .expect("crawl run");

    let conn = rusqlite::Connection::open(tmp.path().join("db.sqlite3")).unwrap();

    // The foreign bytes must not enter the corpus under our URL ...
    let raw: i64 = conn
        .query_row("SELECT COUNT(*) FROM raw_docs", [], |r| r.get(0))
        .unwrap();
    assert_eq!(raw, 1, "only the seed's own body may be stored");
    let sha: Option<String> = conn
        .query_row(
            "SELECT content_sha256 FROM queue WHERE url='http://site.test/go'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(
        sha, None,
        "no digest may be recorded for off-domain content"
    );

    // ... it is counted as skipped, not as a successful fetch ...
    assert_eq!(
        counts["site.test"].skipped, 1,
        "off-domain redirect is skipped"
    );
    assert_eq!(counts["site.test"].new, 1, "only the seed is new");

    // ... the reason is auditable ...
    let err: Option<String> = conn
        .query_row(
            "SELECT error FROM crawl_log WHERE url='http://site.test/go'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(
        err.unwrap_or_default().contains("evil.test"),
        "crawl_log must record where it was redirected"
    );

    // ... and no links from the foreign page leak into our graph.
    let edges: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM links WHERE src_url='http://site.test/go'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(
        edges, 0,
        "foreign page's links must not be recorded as ours"
    );
}

#[test]
fn max_pages_caps_total_fetches() {
    let tmp = tempfile::tempdir().unwrap();
    let mut cfg = config(tmp.path());
    cfg.max_pages = 2;
    let counts = run_with_client(
        cfg,
        "run-cap".into(),
        false,
        ProgressSink::new(None),
        fixture(),
    )
    .expect("crawl run");
    assert_eq!(counts["site.test"].fetched, 2, "max_pages caps fetches");
}

#[test]
fn second_run_new_only_fetches_nothing() {
    let tmp = tempfile::tempdir().unwrap();
    // First full crawl populates the DB.
    let first = run(tmp.path());
    assert_eq!(first["site.test"].fetched, 5);

    // A new-only re-run must not re-fetch any already-checked URL.
    let mut cfg = config(tmp.path());
    cfg.recheck = "new-only".into();
    let second = run_with_client(
        cfg,
        "run-2".into(),
        false,
        ProgressSink::new(None),
        fixture(),
    )
    .expect("crawl run");
    assert_eq!(second["site.test"].fetched, 0, "nothing new to fetch");
}

#[test]
fn per_host_budget_caps_single_host() {
    let tmp = tempfile::tempdir().unwrap();
    let cfg = RunConfig {
        sites: vec![SiteCfg {
            name: "t".into(),
            seed_url: "http://hub.test/start".into(),
            allowed_domain: "test".into(),
        }],
        use_sitemap: false,
        max_pages: 0,
        max_pages_per_host: 2,
        request_delay_seconds: 0.0,
        workers_per_host: 4,
        recheck: "all".into(),
        user_agent: "test".into(),
        db_file: tmp.path().join("db.sqlite3").to_string_lossy().into_owned(),
        raw_dir: tmp.path().join("raw").to_string_lossy().into_owned(),
    };
    // hub links to 4 pages on flood.test and 2 on good.test; both hosts are
    // in-domain for allowed_domain "test". With max_pages_per_host=2, flood.test is
    // capped at 2 while good.test (under the cap) is crawled fully.
    let client = MockClient::new(vec![
        (
            "http://hub.test/start",
            "text/html",
            r#"<a href="http://flood.test/1">1</a>
               <a href="http://flood.test/2">2</a>
               <a href="http://flood.test/3">3</a>
               <a href="http://flood.test/4">4</a>
               <a href="http://good.test/a">a</a>
               <a href="http://good.test/b">b</a>"#,
        ),
        ("http://flood.test/1", "text/html", "<body>f1</body>"),
        ("http://flood.test/2", "text/html", "<body>f2</body>"),
        ("http://flood.test/3", "text/html", "<body>f3</body>"),
        ("http://flood.test/4", "text/html", "<body>f4</body>"),
        ("http://good.test/a", "text/html", "<body>ga</body>"),
        ("http://good.test/b", "text/html", "<body>gb</body>"),
    ]);
    let counts = run_with_client(
        cfg,
        "run-perhost".into(),
        false,
        ProgressSink::new(None),
        client,
    )
    .expect("crawl run");
    // seed hub(1) + flood(2, capped) + good(2) = 5
    assert_eq!(
        counts["test"].fetched, 5,
        "per-host cap limits total fetched"
    );

    let conn = rusqlite::Connection::open(tmp.path().join("db.sqlite3")).unwrap();
    let done = |like: &str| -> i64 {
        conn.query_row(
            "SELECT COUNT(*) FROM queue WHERE work_state='done' AND url LIKE ?",
            [like],
            |r| r.get(0),
        )
        .unwrap()
    };
    assert_eq!(done("http://flood.test/%"), 2, "flood.test capped at 2");
    assert_eq!(done("http://good.test/%"), 2, "good.test unaffected by cap");
    // The 2 over-budget flood URLs stay pending (available to a later, higher-cap run).
    let flood_pending: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM queue WHERE work_state='pending' AND url LIKE 'http://flood.test/%'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(flood_pending, 2, "over-budget flood URLs remain pending");
}

#[test]
fn frontier_load_drops_preseeded_trap() {
    let tmp = tempfile::tempdir().unwrap();
    let db_path = tmp.path().join("db.sqlite3");
    // Pre-seed the queue with a trap URL already pending (as if enqueued before the
    // trap rule existed, or via a sitemap). The frontier load must drop it so it is
    // never served/fetched — the block is authoritative, not just discovery-time.
    {
        let conn = _native::storage::connect(db_path.to_str().unwrap()).unwrap();
        _native::storage::init_db(&conn).unwrap();
        _native::storage::enqueue(
            &conn,
            "https://buchen.dhbw-vs.de/edit_entry.php?area=5&room=10",
            "dhbw-vs.de",
            1,
            Some("https://www.dhbw-vs.de/start"),
            "2026-07-16T00:00:00",
        )
        .unwrap();
    }
    let cfg = RunConfig {
        sites: vec![SiteCfg {
            name: "vs".into(),
            seed_url: "https://www.dhbw-vs.de/start".into(),
            allowed_domain: "dhbw-vs.de".into(),
        }],
        use_sitemap: false,
        max_pages: 0,
        max_pages_per_host: 0,
        request_delay_seconds: 0.0,
        workers_per_host: 2,
        recheck: "all".into(),
        user_agent: "test".into(),
        db_file: db_path.to_string_lossy().into_owned(),
        raw_dir: tmp.path().join("raw").to_string_lossy().into_owned(),
    };
    let client = MockClient::new(vec![
        (
            "https://www.dhbw-vs.de/start",
            "text/html",
            r#"<body>seed <a href="/studium">real</a></body>"#,
        ),
        (
            "https://www.dhbw-vs.de/studium",
            "text/html",
            "<body>studium</body>",
        ),
    ]);
    run_with_client(
        cfg,
        "run-trap".into(),
        false,
        ProgressSink::new(None),
        client,
    )
    .expect("crawl run");

    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let logged: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM crawl_log WHERE url LIKE 'https://buchen.dhbw-vs.de/%'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(logged, 0, "pre-seeded trap must never be fetched");
    let still_pending: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM queue WHERE url LIKE 'https://buchen.dhbw-vs.de/%' AND work_state='pending'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(still_pending, 1, "trap row left pending, untouched");
}
