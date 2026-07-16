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

#[derive(Clone)]
struct MockClient {
    pages: Arc<HashMap<String, (String, Vec<u8>)>>, // url -> (content_type, body)
}

impl MockClient {
    fn new(pages: Vec<(&str, &str, &str)>) -> Self {
        let map = pages
            .into_iter()
            .map(|(url, ct, body)| (url.to_string(), (ct.to_string(), body.as_bytes().to_vec())))
            .collect();
        Self {
            pages: Arc::new(map),
        }
    }
}

impl HttpClient for MockClient {
    async fn fetch(&self, req: FetchRequest, _ua: String) -> FetchResult {
        match self.pages.get(&req.url) {
            Some((ct, body)) => FetchResult {
                url: req.url.clone(),
                final_url: req.url,
                status: 200,
                content_type: ct.clone(),
                data: body.clone(),
                etag: None,
                last_modified: None,
                error: None,
            },
            None => FetchResult {
                url: req.url.clone(),
                final_url: req.url,
                status: 404,
                content_type: String::new(),
                data: Vec::new(),
                etag: None,
                last_modified: None,
                error: Some("HTTP 404".into()),
            },
        }
    }

    async fn fetch_bytes(&self, url: String, _ua: String) -> Option<Vec<u8>> {
        self.pages.get(&url).map(|(_, body)| body.clone())
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
            .query_row("SELECT COUNT(*) FROM queue WHERE url=?", [absent], |r| r.get(0))
            .unwrap();
        assert_eq!(n, 0, "{absent} must not be enqueued");
    }

    // Links edge table: the full outbound set from the seed, incl. external
    // (in_domain=0) and the trap (recorded but never followed).
    let ext: i64 = conn
        .query_row(
            "SELECT in_domain FROM links WHERE src_url=? AND dst_url=?",
            [
                "http://site.test/startseite",
                "http://external.test/x",
            ],
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

#[test]
fn max_pages_caps_total_fetches() {
    let tmp = tempfile::tempdir().unwrap();
    let mut cfg = config(tmp.path());
    cfg.max_pages = 2;
    let counts = run_with_client(cfg, "run-cap".into(), false, ProgressSink::new(None), fixture())
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
    let second =
        run_with_client(cfg, "run-2".into(), false, ProgressSink::new(None), fixture())
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
    let counts =
        run_with_client(cfg, "run-perhost".into(), false, ProgressSink::new(None), client)
            .expect("crawl run");
    // seed hub(1) + flood(2, capped) + good(2) = 5
    assert_eq!(counts["test"].fetched, 5, "per-host cap limits total fetched");

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
    run_with_client(cfg, "run-trap".into(), false, ProgressSink::new(None), client)
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
