//! End-to-end orchestration tests using an in-memory `HttpClient` (the Rust analogue of the Python `fetch_fn` injection in `test_crawl.py`).

use std::collections::HashMap;
use std::sync::Arc;

use _engine::config::{RunConfig, SiteCfg};
use _engine::crawl::run_with_client;
use _engine::fetch::{FetchRequest, FetchResult, HttpClient};
use _engine::progress::ProgressSink;

/// One canned mock HTTP response, defaulting to a 200 with an empty body.
#[derive(Clone, Default)]
struct Page {
    content_type: String,
    body: Vec<u8>,
    status: u16,
    final_url: Option<String>,
    etag: Option<String>,
    revalidates: bool,
    etag_on_304: Option<String>,
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

    /// Respond with this status instead of 200.
    fn status(mut self, status: u16) -> Self {
        self.status = status;
        self
    }

    /// Serve this ETag, and answer 304 to a matching conditional GET.
    fn etag(mut self, etag: &str) -> Self {
        self.etag = Some(etag.into());
        self.revalidates = true;
        self
    }

    /// The 304 rotates its validator to this new ETag.
    fn rotates_etag_to(mut self, etag: &str) -> Self {
        self.etag_on_304 = Some(etag.into());
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
        if page.revalidates && req.etag.is_some() && req.etag == page.etag {
            return FetchResult {
                url: req.url.clone(),
                final_url: req.url,
                status: 304,
                content_type: String::new(),
                data: Vec::new(),
                etag: page.etag_on_304.clone(),
                last_modified: None,
                error: None,
            };
        }
        let status = if page.status == 0 { 200 } else { page.status };
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
            etag: page.etag.clone(),
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
        retry_transient_errors: true,
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

fn run(dir: &std::path::Path) -> HashMap<String, _engine::writer::Counts> {
    run_with_client(
        config(dir),
        "run-test".into(),
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
    assert_eq!(c.fetched, 5, "fetched count");
    assert_eq!(c.new, 5, "all new on a cold crawl");
    assert_eq!(c.error, 0);

    let conn = rusqlite::Connection::open(tmp.path().join("db.sqlite3")).unwrap();

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

    let ext: i64 = conn
        .query_row(
            "SELECT l.in_domain FROM links l
                 JOIN urls s ON s.id = l.src_id JOIN urls d ON d.id = l.dst_id
             WHERE s.url=? AND d.url=?",
            ["http://site.test/startseite", "http://external.test/x"],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(ext, 0, "external edge recorded with in_domain=0");
    let trap: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM links l JOIN urls d ON d.id = l.dst_id WHERE d.url=?",
            ["http://site.test/calendar/view.php?view=month&time=1"],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(trap, 1, "trap edge recorded even though never crawled");

    let raw_pending: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM raw_docs WHERE extract_state='pending'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(raw_pending, 5);

    let files = std::fs::read_dir(tmp.path().join("raw")).unwrap().count();
    assert_eq!(files, 5);
}

/// A raw-cache write failure must never leave a page claiming a stored digest it has no bytes for.
#[test]
fn raw_cache_write_failure_never_orphans_a_page() {
    let tmp = tempfile::tempdir().unwrap();
    std::fs::write(tmp.path().join("raw"), b"not a directory").unwrap();

    let counts = run_with_client(
        config(tmp.path()),
        "run-rawfail".into(),
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

    let c = &counts["site.test"];
    assert_eq!(c.new, 0, "nothing was stored, so nothing is 'new'");
    assert!(c.error > 0, "raw-write failures must surface as errors");

    assert_eq!(
        c.fetched, 5,
        "discovered links must still be followed after a raw-write failure"
    );
    let edges: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM links l JOIN urls s ON s.id = l.src_id
             WHERE s.url = 'http://site.test/startseite'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(
        edges > 0,
        "edges discovered from the fetched body must survive"
    );
}

/// An in-domain URL that redirects to a foreign host must not have that host's content stored.
#[test]
fn off_domain_redirect_content_is_not_stored() {
    let tmp = tempfile::tempdir().unwrap();
    let client = MockClient::from_pages(vec![
        (
            "http://site.test/startseite",
            Page::html(r#"<html><body>seed <a href="/go">go</a></body></html>"#),
        ),
        (
            "http://site.test/go",
            Page::html("<html><body>foreign host content, not ours at all</body></html>")
                .redirected_to("http://evil.test/landing"),
        ),
    ]);
    let mut cfg = config(tmp.path());
    cfg.use_sitemap = false;

    let counts = run_with_client(cfg, "run-redir".into(), ProgressSink::new(None), client)
        .expect("crawl run");

    let conn = rusqlite::Connection::open(tmp.path().join("db.sqlite3")).unwrap();

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

    assert_eq!(
        counts["site.test"].skipped, 1,
        "off-domain redirect is skipped"
    );
    assert_eq!(counts["site.test"].new, 1, "only the seed is new");

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

    let edges: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM links l JOIN urls s ON s.id = l.src_id
             WHERE s.url='http://site.test/go'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(
        edges, 0,
        "foreign page's links must not be recorded as ours"
    );
}

/// A legitimate empty 200/204 must not be recorded as an error.
#[test]
fn empty_body_2xx_is_not_recorded_as_an_error() {
    let tmp = tempfile::tempdir().unwrap();
    let mut cfg = config(tmp.path());
    cfg.use_sitemap = false;
    let client = MockClient::from_pages(vec![
        (
            "http://site.test/startseite",
            Page::html(r#"<html><body>seed <a href="/empty">e</a></body></html>"#),
        ),
        ("http://site.test/empty", Page::html("")),
    ]);

    let counts = run_with_client(cfg, "run-empty".into(), ProgressSink::new(None), client)
        .expect("crawl run");

    let conn = rusqlite::Connection::open(tmp.path().join("db.sqlite3")).unwrap();
    let (state, status): (String, Option<i64>) = conn
        .query_row(
            "SELECT work_state, http_status FROM queue WHERE url='http://site.test/empty'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(state, "done", "an empty 200 is not an error");
    assert_eq!(status, Some(200));
    assert_eq!(
        counts["site.test"].error, 0,
        "no errors on a clean empty 200"
    );
    assert_eq!(counts["site.test"].skipped, 1);
    let err: Option<String> = conn
        .query_row(
            "SELECT error FROM crawl_log WHERE url='http://site.test/empty'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(
        err.unwrap_or_default().contains("empty"),
        "crawl_log must say why nothing was stored"
    );
}

/// A page that already has content keeps it rather than being wiped when it later answers an empty body.
#[test]
fn empty_body_2xx_keeps_previously_stored_content() {
    let tmp = tempfile::tempdir().unwrap();
    let page = "http://site.test/p";
    let seed = r#"<html><body>seed <a href="/p">p</a></body></html>"#;

    let mut cfg = config(tmp.path());
    cfg.use_sitemap = false;
    run_with_client(
        cfg,
        "run-1".into(),
        ProgressSink::new(None),
        MockClient::from_pages(vec![
            ("http://site.test/startseite", Page::html(seed)),
            (
                page,
                Page::html("<html><body>real content here</body></html>"),
            ),
        ]),
    )
    .expect("first crawl");

    let conn = rusqlite::Connection::open(tmp.path().join("db.sqlite3")).unwrap();
    let before: Option<String> = conn
        .query_row(
            "SELECT content_sha256 FROM queue WHERE url=?",
            [page],
            |r| r.get(0),
        )
        .unwrap();
    assert!(before.is_some(), "precondition: the page stored content");

    let mut cfg2 = config(tmp.path());
    cfg2.use_sitemap = false;
    let counts = run_with_client(
        cfg2,
        "run-2".into(),
        ProgressSink::new(None),
        MockClient::from_pages(vec![
            ("http://site.test/startseite", Page::html(seed)),
            (page, Page::html("")),
        ]),
    )
    .expect("second crawl");

    let after: Option<String> = conn
        .query_row(
            "SELECT content_sha256 FROM queue WHERE url=?",
            [page],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(
        after, before,
        "a blank response must not drop stored content"
    );
    assert_eq!(counts["site.test"].error, 0, "still not an error");
    let orphans: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM queue q WHERE q.present=1 AND q.content_sha256 IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM raw_docs r WHERE r.content_sha256=q.content_sha256)",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(orphans, 0, "the kept digest must still resolve to its blob");
}

/// A 410 Gone must be recorded as 410, not flattened to 404.
#[test]
fn gone_410_is_recorded_as_410_not_404() {
    let tmp = tempfile::tempdir().unwrap();
    let mut cfg = config(tmp.path());
    cfg.use_sitemap = false;
    let client = MockClient::from_pages(vec![
        (
            "http://site.test/startseite",
            Page::html(r#"<html><body>seed <a href="/gone">g</a></body></html>"#),
        ),
        (
            "http://site.test/gone",
            Page::html("<html><body>gone for good</body></html>").status(410),
        ),
    ]);

    run_with_client(cfg, "run-410".into(), ProgressSink::new(None), client).expect("crawl run");

    let conn = rusqlite::Connection::open(tmp.path().join("db.sqlite3")).unwrap();
    let (status, present): (i64, i64) = conn
        .query_row(
            "SELECT http_status, present FROM queue WHERE url='http://site.test/gone'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(
        status, 410,
        "a 410 must be stored as 410, not flattened to 404"
    );
    assert_eq!(present, 0, "410 still removes the page");
}

/// A 304 carrying a rotated ETag must adopt the new validator.
#[test]
fn a_304_adopts_its_rotated_etag() {
    let tmp = tempfile::tempdir().unwrap();
    let page = "http://site.test/p";
    let seed = r#"<html><body>seed <a href="/p">p</a></body></html>"#;

    let mut cfg = config(tmp.path());
    cfg.use_sitemap = false;
    run_with_client(
        cfg,
        "run-1".into(),
        ProgressSink::new(None),
        MockClient::from_pages(vec![
            ("http://site.test/startseite", Page::html(seed)),
            (
                page,
                Page::html("<html><body>body</body></html>").etag("\"v1\""),
            ),
        ]),
    )
    .expect("first crawl");

    let conn = rusqlite::Connection::open(tmp.path().join("db.sqlite3")).unwrap();
    let stored: Option<String> = conn
        .query_row("SELECT etag FROM queue WHERE url=?", [page], |r| r.get(0))
        .unwrap();
    assert_eq!(stored.as_deref(), Some("\"v1\""), "precondition");

    let mut cfg2 = config(tmp.path());
    cfg2.use_sitemap = false;
    run_with_client(
        cfg2,
        "run-2".into(),
        ProgressSink::new(None),
        MockClient::from_pages(vec![
            ("http://site.test/startseite", Page::html(seed)),
            (
                page,
                Page::html("<html><body>body</body></html>")
                    .etag("\"v1\"")
                    .rotates_etag_to("\"v2\""),
            ),
        ]),
    )
    .expect("second crawl");

    let after: Option<String> = conn
        .query_row("SELECT etag FROM queue WHERE url=?", [page], |r| r.get(0))
        .unwrap();
    assert_eq!(
        after.as_deref(),
        Some("\"v2\""),
        "a 304's rotated ETag must be adopted, not thrown away"
    );
}

/// A 304 must re-emit the page's edges from its cached blob.
#[test]
fn a_304_re_emits_edges_from_the_cached_blob() {
    let tmp = tempfile::tempdir().unwrap();
    let seed = "http://site.test/startseite";
    let pages = || {
        MockClient::from_pages(vec![
            (
                seed,
                Page::html(r#"<html><body>seed <a href="/a">a</a></body></html>"#).etag("\"s1\""),
            ),
            (
                "http://site.test/a",
                Page::html(r#"<html><body>a <a href="/b">b</a></body></html>"#).etag("\"a1\""),
            ),
            (
                "http://site.test/b",
                Page::html("<html><body>b leaf</body></html>").etag("\"b1\""),
            ),
        ])
    };

    let mut cfg = config(tmp.path());
    cfg.use_sitemap = false;
    run_with_client(cfg, "run-1".into(), ProgressSink::new(None), pages()).expect("first crawl");

    let conn = rusqlite::Connection::open(tmp.path().join("db.sqlite3")).unwrap();
    let before: i64 = conn
        .query_row("SELECT COUNT(*) FROM links", [], |r| r.get(0))
        .unwrap();
    assert!(
        before > 0,
        "precondition: the full-body crawl recorded edges"
    );

    conn.execute("DELETE FROM links", []).unwrap();

    let mut cfg2 = config(tmp.path());
    cfg2.use_sitemap = false;
    let counts = run_with_client(cfg2, "run-2".into(), ProgressSink::new(None), pages())
        .expect("second crawl");
    assert!(
        counts["site.test"].unchanged > 0,
        "precondition: the re-crawl really did revalidate as 304"
    );

    let after: i64 = conn
        .query_row("SELECT COUNT(*) FROM links", [], |r| r.get(0))
        .unwrap();
    assert_eq!(
        after, before,
        "a 304 must re-emit the page's edges from its cached blob"
    );
    let seed_to_a: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM links l
                 JOIN urls s ON s.id = l.src_id JOIN urls d ON d.id = l.dst_id
             WHERE s.url=? AND d.url='http://site.test/a'",
            [seed],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(seed_to_a, 1, "the specific edge is back");
}

#[test]
fn max_pages_caps_total_fetches() {
    let tmp = tempfile::tempdir().unwrap();
    let mut cfg = config(tmp.path());
    cfg.max_pages = 2;
    let counts = run_with_client(cfg, "run-cap".into(), ProgressSink::new(None), fixture())
        .expect("crawl run");
    assert_eq!(counts["site.test"].fetched, 2, "max_pages caps fetches");
}

#[test]
fn second_run_new_only_fetches_nothing() {
    let tmp = tempfile::tempdir().unwrap();
    let first = run(tmp.path());
    assert_eq!(first["site.test"].fetched, 5);

    let mut cfg = config(tmp.path());
    cfg.recheck = "new-only".into();
    let second = run_with_client(cfg, "run-2".into(), ProgressSink::new(None), fixture())
        .expect("crawl run");
    assert_eq!(second["site.test"].fetched, 0, "nothing new to fetch");
}

/// `recheck = "force-full"` is `"all"` plus: do not send the stored validators.
#[test]
fn force_full_redownloads_where_recheck_all_revalidates() {
    let tmp = tempfile::tempdir().unwrap();
    let seed = "http://site.test/startseite";
    let pages = || {
        MockClient::from_pages(vec![(
            seed,
            Page::html("<html><body>seed body</body></html>").etag("\"v1\""),
        )])
    };
    let cfg_at = |recheck: &str| {
        let mut c = config(tmp.path());
        c.use_sitemap = false;
        c.recheck = recheck.into();
        c
    };

    run_with_client(
        cfg_at("all"),
        "run-1".into(),
        ProgressSink::new(None),
        pages(),
    )
    .expect("first crawl");
    run_with_client(
        cfg_at("all"),
        "run-2".into(),
        ProgressSink::new(None),
        pages(),
    )
    .expect("recheck=all crawl");
    run_with_client(
        cfg_at("force-full"),
        "run-3".into(),
        ProgressSink::new(None),
        pages(),
    )
    .expect("force-full crawl");

    let conn = rusqlite::Connection::open(tmp.path().join("db.sqlite3")).unwrap();
    let status = |run: &str| -> i64 {
        conn.query_row(
            "SELECT status FROM crawl_log WHERE run_id=? AND url=?",
            [run, seed],
            |r| r.get(0),
        )
        .unwrap()
    };
    assert_eq!(
        status("run-2"),
        304,
        "precondition: recheck=all revalidates"
    );
    assert_eq!(status("run-3"), 200, "force-full must re-download, not 304");
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
        retry_transient_errors: true,
        user_agent: "test".into(),
        db_file: tmp.path().join("db.sqlite3").to_string_lossy().into_owned(),
        raw_dir: tmp.path().join("raw").to_string_lossy().into_owned(),
    };
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
    let counts = run_with_client(cfg, "run-perhost".into(), ProgressSink::new(None), client)
        .expect("crawl run");
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
    {
        let conn = _engine::storage::connect(db_path.to_str().unwrap()).unwrap();
        _engine::storage::init_db(&conn).unwrap();
        _engine::storage::enqueue(
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
        retry_transient_errors: true,
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
    run_with_client(cfg, "run-trap".into(), ProgressSink::new(None), client).expect("crawl run");

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

/// Run a first crawl in `dir` where the seed links to `path` and `path` answers `status`, so it lands in the queue as an error row.
fn first_error_run(dir: &std::path::Path, path: &str, status: u16) {
    let mut cfg = config(dir);
    cfg.use_sitemap = false;
    let url = format!("http://site.test{path}");
    let seed = format!(r#"<html><body>seed <a href="{path}">c</a></body></html>"#);
    let client = MockClient::from_pages(vec![
        ("http://site.test/startseite", Page::html(&seed)),
        (&url, Page::html("temporarily unavailable").status(status)),
    ]);
    run_with_client(cfg, "run-1".into(), ProgressSink::new(None), client).expect("first crawl");
}

fn error_row(conn: &rusqlite::Connection, url: &str) -> (String, i64) {
    conn.query_row(
        "SELECT work_state, http_status FROM queue WHERE url=?",
        [url],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )
    .unwrap()
}

fn refetched_in_run(conn: &rusqlite::Connection, run_id: &str, url: &str) -> bool {
    let n: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM crawl_log WHERE run_id=? AND url=?",
            [run_id, url],
            |r| r.get(0),
        )
        .unwrap();
    n > 0
}

/// A transient failure (503) is re-queued on the next run and, if it now succeeds, becomes a normal present document.
#[test]
fn transient_error_is_retried_on_rerun() {
    let tmp = tempfile::tempdir().unwrap();
    first_error_run(tmp.path(), "/flap", 503);

    let conn = rusqlite::Connection::open(tmp.path().join("db.sqlite3")).unwrap();
    let (state, status) = error_row(&conn, "http://site.test/flap");
    assert_eq!(state, "error", "a 503 is a transient error");
    assert_eq!(status, 503);

    let mut cfg2 = config(tmp.path());
    cfg2.use_sitemap = false;
    let seed = r#"<html><body>seed <a href="/flap">f</a></body></html>"#;
    run_with_client(
        cfg2,
        "run-2".into(),
        ProgressSink::new(None),
        MockClient::from_pages(vec![
            ("http://site.test/startseite", Page::html(seed)),
            (
                "http://site.test/flap",
                Page::html("<html><body>back up now</body></html>"),
            ),
        ]),
    )
    .expect("second crawl");

    let (state, present, sha): (String, i64, Option<String>) = conn
        .query_row(
            "SELECT work_state, present, content_sha256 FROM queue
             WHERE url='http://site.test/flap'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .unwrap();
    assert_eq!(
        state, "done",
        "the transient error was retried and recovered"
    );
    assert_eq!(present, 1);
    assert!(sha.is_some(), "the recovered page stored its content");
    assert!(
        refetched_in_run(&conn, "run-2", "http://site.test/flap"),
        "run 2 must have actually re-fetched /flap"
    );
}

/// A permanent client error (403) is NOT re-queued, even on a recheck=all run and even if the URL would now serve 200.
#[test]
fn permanent_error_is_not_retried_on_rerun() {
    let tmp = tempfile::tempdir().unwrap();
    first_error_run(tmp.path(), "/forbidden", 403);

    let conn = rusqlite::Connection::open(tmp.path().join("db.sqlite3")).unwrap();
    let (state, status) = error_row(&conn, "http://site.test/forbidden");
    assert_eq!(state, "error");
    assert_eq!(status, 403);

    let mut cfg2 = config(tmp.path());
    cfg2.use_sitemap = false;
    let seed = r#"<html><body>seed <a href="/forbidden">x</a></body></html>"#;
    run_with_client(
        cfg2,
        "run-2".into(),
        ProgressSink::new(None),
        MockClient::from_pages(vec![
            ("http://site.test/startseite", Page::html(seed)),
            (
                "http://site.test/forbidden",
                Page::html("<html><body>now allowed</body></html>"),
            ),
        ]),
    )
    .expect("second crawl");

    assert_eq!(
        error_row(&conn, "http://site.test/forbidden").0,
        "error",
        "a 403 must stay error across re-runs"
    );
    assert!(
        !refetched_in_run(&conn, "run-2", "http://site.test/forbidden"),
        "a permanent error must not be re-fetched"
    );
}

/// retry_transient_errors=false freezes error rows: even a 503 is left alone.
#[test]
fn retry_transient_errors_false_freezes_error_rows() {
    let tmp = tempfile::tempdir().unwrap();
    first_error_run(tmp.path(), "/flap", 503);

    let conn = rusqlite::Connection::open(tmp.path().join("db.sqlite3")).unwrap();
    assert_eq!(error_row(&conn, "http://site.test/flap").0, "error");

    let mut cfg2 = config(tmp.path());
    cfg2.use_sitemap = false;
    cfg2.retry_transient_errors = false;
    let seed = r#"<html><body>seed <a href="/flap">f</a></body></html>"#;
    run_with_client(
        cfg2,
        "run-2".into(),
        ProgressSink::new(None),
        MockClient::from_pages(vec![
            ("http://site.test/startseite", Page::html(seed)),
            (
                "http://site.test/flap",
                Page::html("<html><body>back up now</body></html>"),
            ),
        ]),
    )
    .expect("second crawl");

    assert_eq!(
        error_row(&conn, "http://site.test/flap").0,
        "error",
        "the toggle being off must freeze the transient error"
    );
    assert!(
        !refetched_in_run(&conn, "run-2", "http://site.test/flap"),
        "with the toggle off nothing is retried"
    );
}

/// recheck=new-only never retries error rows (they have last_checked_at set), so the transient requeue is skipped there.
#[test]
fn new_only_does_not_retry_transient_errors() {
    let tmp = tempfile::tempdir().unwrap();
    first_error_run(tmp.path(), "/flap", 503);

    let conn = rusqlite::Connection::open(tmp.path().join("db.sqlite3")).unwrap();
    assert_eq!(error_row(&conn, "http://site.test/flap").0, "error");

    let mut cfg2 = config(tmp.path());
    cfg2.use_sitemap = false;
    cfg2.recheck = "new-only".into();
    let seed = r#"<html><body>seed <a href="/flap">f</a></body></html>"#;
    run_with_client(
        cfg2,
        "run-2".into(),
        ProgressSink::new(None),
        MockClient::from_pages(vec![
            ("http://site.test/startseite", Page::html(seed)),
            (
                "http://site.test/flap",
                Page::html("<html><body>back up now</body></html>"),
            ),
        ]),
    )
    .expect("second crawl");

    assert_eq!(
        error_row(&conn, "http://site.test/flap").0,
        "error",
        "new-only must not retry an already-checked error row"
    );
    assert!(
        !refetched_in_run(&conn, "run-2", "http://site.test/flap"),
        "new-only must not re-fetch the error row"
    );
}
