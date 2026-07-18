//! Port of the Python `tests/test_sitemap.py`, using an in-memory mock `HttpClient` in place of the injected `fetch_fn`.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use _engine::fetch::{FetchRequest, FetchResult, HttpClient};
use _engine::sitemap;

#[derive(Clone)]
struct MockClient {
    pages: Arc<HashMap<String, Vec<u8>>>,
    calls: Arc<Mutex<Vec<String>>>,
}

impl MockClient {
    fn new(pages: &[(&str, &[u8])]) -> Self {
        let map = pages
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_vec()))
            .collect();
        Self {
            pages: Arc::new(map),
            calls: Arc::new(Mutex::new(Vec::new())),
        }
    }
}

impl HttpClient for MockClient {
    async fn fetch(&self, req: FetchRequest, _ua: String) -> FetchResult {
        FetchResult {
            url: req.url.clone(),
            final_url: req.url,
            status: 404,
            content_type: String::new(),
            data: Vec::new(),
            etag: None,
            last_modified: None,
            error: Some("unused".into()),
        }
    }

    async fn fetch_bytes(&self, url: String, _ua: String) -> Option<Vec<u8>> {
        self.calls.lock().unwrap().push(url.clone());
        self.pages.get(&url).cloned()
    }
}

const INDEX: &[u8] = br#"<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.dhbw.de/sitemap-1.xml</loc></sitemap>
</sitemapindex>"#;

const URLSET: &[u8] = br#"<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.dhbw.de/studium</loc><lastmod>2026-02-01</lastmod></url>
  <url><loc>https://other.example/x</loc></url>
</urlset>"#;

#[tokio::test]
async fn discover_follows_index_and_filters_domain() {
    let client = MockClient::new(&[
        ("https://www.dhbw.de/sitemap.xml", INDEX),
        ("https://www.dhbw.de/sitemap-1.xml", URLSET),
    ]);
    let pairs = sitemap::discover("https://www.dhbw.de", "www.dhbw.de", &client, "ua").await;
    let urls: HashMap<_, _> = pairs.iter().cloned().collect();
    assert!(urls.contains_key("https://www.dhbw.de/studium"));
    assert_eq!(
        urls["https://www.dhbw.de/studium"],
        Some("2026-02-01".to_string())
    );
    assert!(pairs.iter().all(|(u, _)| !u.contains("other.example")));
}

#[tokio::test]
async fn off_domain_index_child_not_followed() {
    let off_domain_index = br#"<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://evil.example/sitemap-x.xml</loc></sitemap>
</sitemapindex>"#;
    let client = MockClient::new(&[
        ("https://www.dhbw.de/sitemap.xml", off_domain_index),
        ("https://evil.example/sitemap-x.xml", URLSET),
    ]);
    let pairs = sitemap::discover("https://www.dhbw.de", "www.dhbw.de", &client, "ua").await;
    assert!(pairs.is_empty());
    let calls = client.calls.lock().unwrap();
    assert!(
        !calls
            .iter()
            .any(|u| u == "https://evil.example/sitemap-x.xml")
    );
}

fn gzipped(data: &[u8]) -> Vec<u8> {
    use flate2::Compression;
    use flate2::write::GzEncoder;
    use std::io::Write;
    let mut enc = GzEncoder::new(Vec::new(), Compression::default());
    enc.write_all(data).unwrap();
    enc.finish().unwrap()
}

/// A gzipped child sitemap served as application/gzip is decompressed so its URLs are discovered.
#[tokio::test]
async fn gzipped_child_sitemap_is_decompressed() {
    let index = br#"<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.dhbw.de/sitemap-1.xml.gz</loc></sitemap>
</sitemapindex>"#;
    let gz = gzipped(URLSET);
    let client = MockClient::new(&[
        ("https://www.dhbw.de/sitemap.xml", index),
        ("https://www.dhbw.de/sitemap-1.xml.gz", &gz),
    ]);

    let pairs = sitemap::discover("https://www.dhbw.de", "www.dhbw.de", &client, "ua").await;

    let urls: HashMap<_, _> = pairs.iter().cloned().collect();
    assert!(
        urls.contains_key("https://www.dhbw.de/studium"),
        "a gzipped child sitemap's URLs must be discovered; got {pairs:?}"
    );
    assert_eq!(
        urls["https://www.dhbw.de/studium"],
        Some("2026-02-01".to_string()),
        "and keep their lastmod"
    );
}

/// A Sitemap: line in robots.txt is followed for discovery.
#[tokio::test]
async fn sitemaps_advertised_in_robots_txt_are_followed() {
    let robots =
        b"User-agent: *\nDisallow: /admin\nSitemap: https://www.dhbw.de/sitemap-custom.xml\n";
    let client = MockClient::new(&[
        ("https://www.dhbw.de/robots.txt", robots),
        ("https://www.dhbw.de/sitemap-custom.xml", URLSET),
    ]);

    let pairs = sitemap::discover("https://www.dhbw.de", "www.dhbw.de", &client, "ua").await;

    let urls: HashMap<_, _> = pairs.iter().cloned().collect();
    assert!(
        urls.contains_key("https://www.dhbw.de/studium"),
        "a Sitemap: line in robots.txt must be followed; got {pairs:?}"
    );
}

/// An off-domain Sitemap: line gets the same treatment as an off-domain index child: recorded nowhere, fetched never.
#[tokio::test]
async fn off_domain_robots_sitemap_is_not_followed() {
    let robots = b"Sitemap: https://evil.example/sitemap-x.xml\n";
    let client = MockClient::new(&[
        ("https://www.dhbw.de/robots.txt", robots),
        ("https://evil.example/sitemap-x.xml", URLSET),
    ]);

    let pairs = sitemap::discover("https://www.dhbw.de", "www.dhbw.de", &client, "ua").await;

    assert!(pairs.is_empty());
    let calls = client.calls.lock().unwrap();
    assert!(!calls.iter().any(|u| u.contains("evil.example")));
}

#[tokio::test]
async fn fetch_failure_degrades_to_empty() {
    let client = MockClient::new(&[]);
    let pairs = sitemap::discover("https://www.dhbw.de", "www.dhbw.de", &client, "ua").await;
    assert!(pairs.is_empty());
}

#[tokio::test]
async fn cycle_safety_terminates() {
    let self_index = br#"<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.dhbw.de/sitemap.xml</loc></sitemap>
</sitemapindex>"#;
    let client = MockClient::new(&[("https://www.dhbw.de/sitemap.xml", self_index)]);
    let pairs = sitemap::discover("https://www.dhbw.de", "www.dhbw.de", &client, "ua").await;
    assert!(pairs.is_empty());
    let calls = client.calls.lock().unwrap();
    assert!(
        calls
            .iter()
            .filter(|u| *u == "https://www.dhbw.de/sitemap.xml")
            .count()
            <= 1
    );
}

#[tokio::test]
async fn entity_escaped_url_is_unescaped() {
    let escaped = br#"<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.dhbw.de/search?a=1&amp;b=2</loc></url>
</urlset>"#;
    let client = MockClient::new(&[("https://www.dhbw.de/sitemap.xml", escaped)]);
    let pairs = sitemap::discover("https://www.dhbw.de", "www.dhbw.de", &client, "ua").await;
    let urls: HashMap<_, _> = pairs.iter().cloned().collect();
    assert!(urls.contains_key("https://www.dhbw.de/search?a=1&b=2"));
}
