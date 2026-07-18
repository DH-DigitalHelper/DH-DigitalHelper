//! Phase-1 orchestration: seed the frontier, run per-host async workers, and funnel every finished page to the single writer/coordinator.

use std::sync::Arc;
use std::time::Duration;

use tokio::sync::mpsc::UnboundedSender;
use tokio::sync::oneshot;

use crate::config::RunConfig;
use crate::fetch::{FetchRequest, FetchResult, HttpClient, ReqwestClient, classify, ext_for};
use crate::links::{discover_all_links, in_domain, is_trap_url};
use crate::outcome::{Outcome, content_outcome};
use crate::progress::ProgressSink;
use crate::storage::{self, FrontierItem, LinkEdge, RawCache, now_iso};
use crate::writer::{
    ClaimResult, Coordinator, Counts, CrawlLogRow, FollowCandidate, PageBatch, RawDocUpsert,
    SiteInit, UrlMark, WriterMsg,
};

#[derive(Debug)]
pub enum CrawlError {
    Sqlite(rusqlite::Error),
    Http(String),
    Io(std::io::Error),
    CoordinatorPanicked,
}

impl std::fmt::Display for CrawlError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CrawlError::Sqlite(e) => write!(f, "sqlite error: {e}"),
            CrawlError::Http(e) => write!(f, "http client error: {e}"),
            CrawlError::Io(e) => write!(f, "io error: {e}"),
            CrawlError::CoordinatorPanicked => write!(f, "writer thread panicked"),
        }
    }
}
impl std::error::Error for CrawlError {}
impl From<rusqlite::Error> for CrawlError {
    fn from(e: rusqlite::Error) -> Self {
        CrawlError::Sqlite(e)
    }
}
impl From<std::io::Error> for CrawlError {
    fn from(e: std::io::Error) -> Self {
        CrawlError::Io(e)
    }
}

/// Per-host request spacing that reserves the next time slot under the lock, then sleeps outside it so other workers are never blocked while this one waits.
struct RateLimiter {
    delay: Duration,
    next: std::sync::Mutex<Option<tokio::time::Instant>>,
}

impl RateLimiter {
    fn new(delay: Duration) -> Self {
        Self {
            delay,
            next: std::sync::Mutex::new(None),
        }
    }

    async fn wait(&self) {
        if self.delay.is_zero() {
            return;
        }
        let scheduled = {
            let mut guard = self.next.lock().unwrap();
            let now = tokio::time::Instant::now();
            let scheduled = match *guard {
                Some(n) => n.max(now),
                None => now,
            };
            *guard = Some(scheduled + self.delay);
            scheduled
        };
        tokio::time::sleep_until(scheduled).await;
    }
}

/// Run the whole Phase-1 crawl with the production reqwest client.
pub fn run(
    config: RunConfig,
    run_id: String,
    progress: ProgressSink,
) -> Result<std::collections::HashMap<String, Counts>, CrawlError> {
    let client = ReqwestClient::new().map_err(|e| CrawlError::Http(e.to_string()))?;
    run_with_client(config, run_id, progress, client)
}

/// Orchestration generic over the HTTP client — the injection seam used by tests in place of the old Python `fetch_fn`.
pub fn run_with_client<C: HttpClient>(
    config: RunConfig,
    run_id: String,
    progress: ProgressSink,
    client: C,
) -> Result<std::collections::HashMap<String, Counts>, CrawlError> {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?;

    let sitemap_results: Vec<Vec<(String, Option<String>)>> = if config.use_sitemap {
        rt.block_on(async {
            let mut handles = Vec::new();
            for site in &config.sites {
                let c = client.clone();
                let seed = site.seed_url.clone();
                let dom = site.allowed_domain.clone();
                let ua = config.user_agent.clone();
                handles.push(tokio::spawn(async move {
                    crate::sitemap::discover(&seed, &dom, &c, &ua).await
                }));
            }
            let mut out = Vec::new();
            for h in handles {
                out.push(h.await.unwrap_or_default());
            }
            out
        })
    } else {
        vec![Vec::new(); config.sites.len()]
    };

    let conn = storage::connect(&config.db_file)?;
    storage::init_db(&conn)?;
    storage::reset_in_progress(&conn)?;
    let now = now_iso();
    for (i, site) in config.sites.iter().enumerate() {
        for (url, lastmod) in &sitemap_results[i] {
            storage::set_sitemap_lastmod(
                &conn,
                url,
                &site.allowed_domain,
                lastmod.as_deref(),
                &now,
            )?;
        }
        storage::enqueue(&conn, &site.seed_url, &site.allowed_domain, 0, None, &now)?;
        if config.rechecks_all() {
            storage::requeue_present_urls(&conn, &site.allowed_domain)?;
        }
        if config.retry_transient_errors && !config.only_new() {
            storage::requeue_transient_errors(&conn, &site.allowed_domain)?;
        }
    }

    let mut inits = Vec::new();
    for site in &config.sites {
        let mut frontier = storage::load_pending(&conn, &site.allowed_domain, config.only_new())?;
        frontier.retain(|it| !is_trap_url(&it.url));
        let seen = storage::all_urls(&conn, &site.allowed_domain)?;
        inits.push(SiteInit {
            name: site.allowed_domain.clone(),
            max_pages: config.max_pages,
            max_pages_per_host: config.max_pages_per_host,
            frontier,
            seen,
        });
    }

    let (tx, rx) = tokio::sync::mpsc::unbounded_channel::<WriterMsg>();
    let coord = Coordinator::new(conn, run_id, inits, progress.try_clone());
    let coord_handle = std::thread::spawn(move || coord.run(rx));

    progress.header("Crawling");

    let force_full = config.force_full();
    rt.block_on(async {
        let mut handles = Vec::new();
        for (i, site) in config.sites.iter().enumerate() {
            let limiter = Arc::new(RateLimiter::new(Duration::from_secs_f64(
                config.request_delay_seconds.max(0.0),
            )));
            for _ in 0..config.workers_per_host() {
                let tx = tx.clone();
                let client = client.clone();
                let cache = RawCache::new(&config.raw_dir);
                let ua = config.user_agent.clone();
                let site_name = site.allowed_domain.clone();
                let lim = limiter.clone();
                handles.push(tokio::spawn(worker(
                    i, site_name, ua, force_full, client, tx, cache, lim,
                )));
            }
        }
        for h in handles {
            let _ = h.await;
        }
    });
    drop(tx);

    let counts = coord_handle
        .join()
        .map_err(|_| CrawlError::CoordinatorPanicked)??;

    for site in &config.sites {
        if let Some(c) = counts.get(&site.allowed_domain) {
            progress.summary(&site.allowed_domain, &c.pairs());
        }
    }
    Ok(counts)
}

#[allow(clippy::too_many_arguments)]
async fn worker<C: HttpClient>(
    site_idx: usize,
    site_name: String,
    user_agent: String,
    force_full: bool,
    client: C,
    tx: UnboundedSender<WriterMsg>,
    cache: RawCache,
    limiter: Arc<RateLimiter>,
) {
    loop {
        let (reply_tx, reply_rx) = oneshot::channel();
        if tx
            .send(WriterMsg::Claim {
                site_idx,
                reply: reply_tx,
            })
            .is_err()
        {
            break;
        }
        let item = match reply_rx.await {
            Ok(ClaimResult::Give(item)) => item,
            Ok(ClaimResult::Done) => break,
            Err(_) => break,
        };

        limiter.wait().await;

        let req = FetchRequest {
            url: item.url.clone(),
            etag: if force_full { None } else { item.etag.clone() },
            last_modified: if force_full {
                None
            } else {
                item.last_modified.clone()
            },
        };
        let result = client.fetch(req, user_agent.clone()).await;
        let batch = build_batch(site_idx, &site_name, &item, result, &cache);
        if tx.send(WriterMsg::Complete(Box::new(batch))).is_err() {
            break;
        }
    }
}

/// Rebuild a page's outbound edges from the copy of its body already on disk.
fn edges_from_cached_blob(
    item: &FrontierItem,
    site_name: &str,
    cache: &RawCache,
    now: &str,
) -> Vec<LinkEdge> {
    let Some(sha) = item.content_sha256.as_deref() else {
        return Vec::new();
    };
    let Ok(data) = std::fs::read(cache.path_for(sha, ext_for("html"))) else {
        return Vec::new();
    };
    let html = String::from_utf8_lossy(&data);
    let depth = item.depth + 1;
    discover_all_links(&html, &item.url)
        .into_iter()
        .map(|dst| {
            let in_dom = in_domain(&dst, site_name);
            LinkEdge {
                src: item.url.clone(),
                dst,
                site: site_name.to_string(),
                in_domain: in_dom,
                depth,
                first_seen_at: now.to_string(),
            }
        })
        .collect()
}

/// Turn a fetch result into the page's complete write-batch.
fn build_batch(
    site_idx: usize,
    site_name: &str,
    item: &FrontierItem,
    result: FetchResult,
    cache: &RawCache,
) -> PageBatch {
    let now = now_iso();

    if result.not_modified() {
        let edges = edges_from_cached_blob(item, site_name, cache, &now);
        return PageBatch {
            site_idx,
            url: item.url.clone(),
            now,
            mark: UrlMark::Checked {
                http_status: 304,
                etag: result.etag.clone().or_else(|| item.etag.clone()),
                last_modified: result
                    .last_modified
                    .clone()
                    .or_else(|| item.last_modified.clone()),
                content_sha256: item.content_sha256.clone(),
                changed: false,
                present: true,
            },
            followable: Vec::new(),
            edges,
            raw_doc: None,
            log: CrawlLogRow {
                final_url: item.url.clone(),
                status: Some(304),
                content_type: None,
                sha256: item.content_sha256.clone(),
                bytes: 0,
                kind: None,
                error: None,
            },
            outcome: Outcome::Unchanged,
        };
    }

    if result.error.is_none() && (200..300).contains(&result.status) && result.data.is_empty() {
        let had_content = item.content_sha256.is_some();
        return PageBatch {
            site_idx,
            url: item.url.clone(),
            now,
            mark: UrlMark::Checked {
                http_status: result.status as i64,
                etag: result.etag.clone(),
                last_modified: result.last_modified.clone(),
                content_sha256: item.content_sha256.clone(),
                changed: false,
                present: true,
            },
            followable: Vec::new(),
            edges: Vec::new(),
            raw_doc: None,
            log: CrawlLogRow {
                final_url: result.final_url.clone(),
                status: Some(result.status as i64),
                content_type: Some(result.content_type.clone()),
                sha256: item.content_sha256.clone(),
                bytes: 0,
                kind: None,
                error: if had_content {
                    None
                } else {
                    Some("empty 2xx body, nothing stored".to_string())
                },
            },
            outcome: if had_content {
                Outcome::Unchanged
            } else {
                Outcome::Skipped
            },
        };
    }

    if !result.ok() {
        let status = result.status as i64;
        if result.status == 404 || result.status == 410 {
            return PageBatch {
                site_idx,
                url: item.url.clone(),
                now,
                mark: UrlMark::Removed {
                    http_status: status,
                },
                followable: Vec::new(),
                edges: Vec::new(),
                raw_doc: None,
                log: CrawlLogRow {
                    final_url: item.url.clone(),
                    status: Some(status),
                    content_type: None,
                    sha256: None,
                    bytes: 0,
                    kind: None,
                    error: result.error.clone(),
                },
                outcome: Outcome::Removed,
            };
        }
        return PageBatch {
            site_idx,
            url: item.url.clone(),
            now,
            mark: UrlMark::Error {
                http_status: Some(status),
            },
            followable: Vec::new(),
            edges: Vec::new(),
            raw_doc: None,
            log: CrawlLogRow {
                final_url: item.url.clone(),
                status: Some(status),
                content_type: None,
                sha256: None,
                bytes: 0,
                kind: None,
                error: result.error.clone(),
            },
            outcome: Outcome::Error,
        };
    }

    if !in_domain(&result.final_url, site_name) {
        return PageBatch {
            site_idx,
            url: item.url.clone(),
            now,
            mark: UrlMark::Checked {
                http_status: 200,
                etag: result.etag.clone(),
                last_modified: result.last_modified.clone(),
                content_sha256: item.content_sha256.clone(),
                changed: false,
                present: true,
            },
            followable: Vec::new(),
            edges: Vec::new(),
            raw_doc: None,
            log: CrawlLogRow {
                final_url: result.final_url.clone(),
                status: Some(200),
                content_type: Some(result.content_type.clone()),
                sha256: None,
                bytes: result.data.len() as i64,
                kind: None,
                error: Some(format!("redirected off-domain to {}", result.final_url)),
            },
            outcome: Outcome::Skipped,
        };
    }

    let kind = classify(&result.content_type, &result.final_url);
    if kind == "other" {
        return PageBatch {
            site_idx,
            url: item.url.clone(),
            now,
            mark: UrlMark::Checked {
                http_status: 200,
                etag: result.etag.clone(),
                last_modified: result.last_modified.clone(),
                content_sha256: item.content_sha256.clone(),
                changed: false,
                present: true,
            },
            followable: Vec::new(),
            edges: Vec::new(),
            raw_doc: None,
            log: CrawlLogRow {
                final_url: result.final_url.clone(),
                status: Some(200),
                content_type: Some(result.content_type.clone()),
                sha256: None,
                bytes: result.data.len() as i64,
                kind: Some("other".to_string()),
                error: None,
            },
            outcome: Outcome::Skipped,
        };
    }

    let digest = storage::sha256_hex(&result.data);
    let (outcome, changed) = content_outcome(item.content_sha256.as_deref(), item.present, &digest);

    let mut followable = Vec::new();
    let mut edges = Vec::new();
    if kind == "html" {
        let html = String::from_utf8_lossy(&result.data);
        let depth = item.depth + 1;
        let all = discover_all_links(&html, &result.final_url);
        for dst in &all {
            let ind = in_domain(dst, site_name);
            edges.push(LinkEdge {
                src: item.url.clone(),
                dst: dst.clone(),
                site: site_name.to_string(),
                in_domain: ind,
                depth,
                first_seen_at: now.clone(),
            });
            if ind && !is_trap_url(dst) {
                followable.push(FollowCandidate {
                    url: dst.clone(),
                    depth,
                });
            }
        }
    }

    let raw_doc = if changed {
        match cache.write(&result.data, ext_for(kind)) {
            Ok((sha, path)) => Some(RawDocUpsert {
                sha,
                source_type: kind.to_string(),
                raw_path: path.to_string_lossy().into_owned(),
                bytes: result.data.len() as i64,
            }),
            Err(e) => {
                return PageBatch {
                    site_idx,
                    url: item.url.clone(),
                    now,
                    mark: UrlMark::Error {
                        http_status: Some(200),
                    },
                    followable,
                    edges,
                    raw_doc: None,
                    log: CrawlLogRow {
                        final_url: result.final_url.clone(),
                        status: Some(200),
                        content_type: Some(result.content_type.clone()),
                        sha256: None,
                        bytes: result.data.len() as i64,
                        kind: Some(kind.to_string()),
                        error: Some(format!("raw cache write failed: {e}")),
                    },
                    outcome: Outcome::Error,
                };
            }
        }
    } else {
        None
    };

    PageBatch {
        site_idx,
        url: item.url.clone(),
        now: now.clone(),
        mark: UrlMark::Checked {
            http_status: 200,
            etag: result.etag.clone(),
            last_modified: result.last_modified.clone(),
            content_sha256: Some(digest.clone()),
            changed,
            present: true,
        },
        followable,
        edges,
        raw_doc,
        log: CrawlLogRow {
            final_url: result.final_url.clone(),
            status: Some(200),
            content_type: Some(result.content_type.clone()),
            sha256: Some(digest),
            bytes: result.data.len() as i64,
            kind: Some(kind.to_string()),
            error: None,
        },
        outcome,
    }
}
