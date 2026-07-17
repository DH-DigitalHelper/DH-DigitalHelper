//! Polite HTTP with conditional GET and content-type routing.
//!
//! Port of the Python `fetch.py`. Failures never panic — they come back on
//! [`FetchResult::error`]. The [`HttpClient`] trait isolates the network so the
//! orchestrator and sitemap discovery are generic over it; production uses
//! [`ReqwestClient`], tests inject a deterministic in-memory client.
//!
//! Note on URL sanitising: the Python `_sanitize_url` re-quoted spaces/control
//! chars left by `urljoin`. Here discovered URLs already come from the `url`
//! crate (which percent-encodes during parse/join) and reqwest is handed a
//! parsed `Url`, so a separate sanitise pass is unnecessary.

use std::future::Future;
use std::time::Duration;

pub const DEFAULT_TIMEOUT_SECS: u64 = 30;

/// A conditional-GET request: stored validators re-sent as If-None-Match /
/// If-Modified-Since so a `304 Not Modified` ends the work with no body.
#[derive(Debug, Clone)]
pub struct FetchRequest {
    pub url: String,
    pub etag: Option<String>,
    pub last_modified: Option<String>,
}

impl FetchRequest {
    pub fn new(url: impl Into<String>) -> Self {
        Self {
            url: url.into(),
            etag: None,
            last_modified: None,
        }
    }
}

/// Outcome of one HTTP fetch. Mirrors the Python `FetchResult` dataclass.
#[derive(Debug, Clone)]
pub struct FetchResult {
    pub url: String,
    pub final_url: String,
    pub status: u16,
    pub content_type: String,
    pub data: Vec<u8>,
    pub etag: Option<String>,
    pub last_modified: Option<String>,
    pub error: Option<String>,
}

impl FetchResult {
    fn error_result(url: &str, status: u16, error: String) -> Self {
        Self {
            url: url.to_string(),
            final_url: url.to_string(),
            status,
            content_type: String::new(),
            data: Vec::new(),
            etag: None,
            last_modified: None,
            error: Some(error),
        }
    }

    /// A usable 2xx response with a non-empty body.
    pub fn ok(&self) -> bool {
        self.error.is_none() && (200..300).contains(&self.status) && !self.data.is_empty()
    }

    pub fn not_modified(&self) -> bool {
        self.status == 304
    }
}

/// Network abstraction. Generic (not `dyn`) so the returned futures stay
/// unboxed; `ReqwestClient` is `Clone` (cheap Arc) for spawning into tasks.
pub trait HttpClient: Clone + Send + Sync + 'static {
    /// Conditional GET for a crawl target.
    fn fetch(
        &self,
        req: FetchRequest,
        user_agent: String,
    ) -> impl Future<Output = FetchResult> + Send;

    /// Unconditional GET returning the body iff the response was ok (used for
    /// sitemap fetches, which carry no stored validators).
    fn fetch_bytes(
        &self,
        url: String,
        user_agent: String,
    ) -> impl Future<Output = Option<Vec<u8>>> + Send;
}

/// reqwest-backed [`HttpClient`]. One shared client = per-host keep-alive pool.
#[derive(Clone)]
pub struct ReqwestClient {
    client: reqwest::Client,
}

impl ReqwestClient {
    pub fn new() -> reqwest::Result<Self> {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(DEFAULT_TIMEOUT_SECS))
            // Follow redirects and expose the final URL (mirrors resp.geturl()).
            .redirect(reqwest::redirect::Policy::limited(10))
            .build()?;
        Ok(Self { client })
    }
}

impl HttpClient for ReqwestClient {
    async fn fetch(&self, req: FetchRequest, user_agent: String) -> FetchResult {
        let mut builder = self
            .client
            .get(&req.url)
            .header(reqwest::header::USER_AGENT, user_agent);
        if let Some(etag) = &req.etag {
            builder = builder.header(reqwest::header::IF_NONE_MATCH, etag);
        }
        if let Some(lm) = &req.last_modified {
            builder = builder.header(reqwest::header::IF_MODIFIED_SINCE, lm);
        }
        let resp = match builder.send().await {
            Ok(r) => r,
            Err(e) => return FetchResult::error_result(&req.url, 0, e.to_string()),
        };
        let status = resp.status().as_u16();
        let final_url = resp.url().to_string();
        let headers = resp.headers();
        let content_type = headers
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .map(mime_essence)
            .unwrap_or_default();
        let etag = header_string(headers, reqwest::header::ETAG);
        let last_modified = header_string(headers, reqwest::header::LAST_MODIFIED);

        if status == 304 {
            return FetchResult {
                url: req.url.clone(),
                final_url: req.url.clone(),
                status: 304,
                content_type: String::new(),
                data: Vec::new(),
                etag: None,
                last_modified: None,
                error: None,
            };
        }
        // Read the body regardless of status; a non-2xx still yields an error
        // string so `removed`/`error` outcomes carry a message in crawl_log.
        let error = if (200..300).contains(&status) {
            None
        } else {
            Some(format!("HTTP {status}"))
        };
        let data = match resp.bytes().await {
            Ok(b) => b.to_vec(),
            Err(e) => return FetchResult::error_result(&req.url, status, e.to_string()),
        };
        FetchResult {
            url: req.url,
            final_url,
            status,
            content_type,
            data,
            etag,
            last_modified,
            error,
        }
    }

    async fn fetch_bytes(&self, url: String, user_agent: String) -> Option<Vec<u8>> {
        let resp = self
            .client
            .get(&url)
            .header(reqwest::header::USER_AGENT, user_agent)
            .send()
            .await
            .ok()?;
        if !resp.status().is_success() {
            return None;
        }
        resp.bytes().await.ok().map(|b| b.to_vec())
    }
}

fn header_string(
    headers: &reqwest::header::HeaderMap,
    name: reqwest::header::HeaderName,
) -> Option<String> {
    headers
        .get(name)
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string())
}

/// The bare media type without parameters, lowercased (mirrors
/// `headers.get_content_type()`), e.g. "text/html; charset=utf-8" -> "text/html".
fn mime_essence(ct: &str) -> String {
    ct.split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase()
}

const BINARY_EXT: &[&str] = &[
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp", ".zip", ".gz", ".tar",
    ".rar", ".7z", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".mp4", ".mp3", ".avi",
    ".mov", ".wav", ".ogg", ".css", ".js", ".json", ".woff", ".woff2", ".ttf", ".eot",
];

/// Route a response to an extractor family: "pdf" | "html" | "other".
/// Verbatim port of Python `classify`.
pub fn classify(content_type: &str, url: &str) -> &'static str {
    let ct = content_type.to_ascii_lowercase();
    let path = url::Url::parse(url)
        .map(|u| u.path().to_ascii_lowercase())
        .unwrap_or_default();
    if ct.contains("pdf") || path.ends_with(".pdf") {
        return "pdf";
    }
    if ct.contains("html") || ct.contains("xml") || ct.starts_with("text/") {
        return "html";
    }
    if !ct.is_empty() {
        return "other";
    }
    if BINARY_EXT.iter().any(|e| path.ends_with(e)) {
        return "other";
    }
    "html"
}

/// File extension for a cached raw blob of the given kind.
pub fn ext_for(kind: &str) -> &'static str {
    match kind {
        "html" => ".html",
        "pdf" => ".pdf",
        _ => ".bin",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classify_pdf_by_content_type_and_extension() {
        assert_eq!(classify("application/pdf", "https://x.de/a"), "pdf");
        assert_eq!(classify("", "https://x.de/a.pdf"), "pdf");
    }

    #[test]
    fn classify_html_by_content_type() {
        assert_eq!(
            classify("text/html; charset=utf-8", "https://x.de/a"),
            "html"
        );
        assert_eq!(classify("application/xml", "https://x.de/a"), "html");
        assert_eq!(classify("text/plain", "https://x.de/a"), "html");
    }

    #[test]
    fn classify_other_by_content_type_or_binary_ext() {
        assert_eq!(classify("image/png", "https://x.de/a"), "other");
        assert_eq!(classify("", "https://x.de/style.css"), "other");
    }

    #[test]
    fn classify_defaults_to_html_when_unknown() {
        assert_eq!(classify("", "https://x.de/page"), "html");
    }

    #[test]
    fn ext_for_maps_kinds() {
        assert_eq!(ext_for("html"), ".html");
        assert_eq!(ext_for("pdf"), ".pdf");
        assert_eq!(ext_for("other"), ".bin");
    }
}
