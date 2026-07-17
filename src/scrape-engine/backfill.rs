//! Offline `links` backfill: rebuild the outbound edge table from raw HTML that
//! is already on disk, with no network fetch.
//!
//! Live crawling emits `links` rows only in the full-body 2xx branch of
//! [`crate::crawl::build_batch`]; a `304 Not Modified` re-validation writes none.
//! So once a page is `done` with stored `ETag`/`Last-Modified` validators, every
//! later re-crawl just 304s it and re-populates nothing — the edge table stays as
//! sparse as whatever single run last fetched each page with a full body.
//!
//! This pass reconstructs those edges from the content-addressed raw blobs the
//! crawl already stored, reusing the exact same [`discover_all_links`] +
//! [`in_domain`] logic the live path uses, so the result matches what a fresh
//! full-body crawl would have written — without re-downloading anything.
//!
//! It is additive and idempotent: edges are inserted `INSERT OR IGNORE` on the
//! `(src_url, dst_url)` primary key, so re-running never duplicates rows and never
//! touches queue, fetch, or extraction state. The relative-link base is the page's
//! own `queue.url`; the live path uses the post-redirect `final_url`, so for a
//! redirected page a handful of relative links could resolve differently, but
//! `url == final_url` for the overwhelming majority of pages.

use crate::config::RunConfig;
use crate::fetch::ext_for;
use crate::links::{discover_all_links, in_domain};
use crate::progress::ProgressSink;
use crate::storage::{self, LinkEdge, RawCache, now_iso};

/// Result of one backfill pass.
#[derive(Debug, Default, Clone, Copy)]
pub struct BackfillCounts {
    /// HTML pages read from disk and re-parsed.
    pub pages: i64,
    /// Edge rows newly inserted (INSERT OR IGNORE hits; already-present edges and
    /// re-runs contribute 0).
    pub edges: i64,
    /// Pages whose `content_sha256` had no readable blob on disk (skipped, never
    /// fatal).
    pub raw_missing: i64,
}

impl BackfillCounts {
    pub fn pairs(&self) -> [(&'static str, i64); 3] {
        [
            ("pages", self.pages),
            ("edges", self.edges),
            ("raw_missing", self.raw_missing),
        ]
    }
}

/// Commit every N pages so a huge corpus neither holds one unbounded transaction
/// nor pays an fsync per page.
const COMMIT_EVERY: usize = 500;

/// Rebuild the `links` table from stored raw HTML. See the module docs.
pub fn run(config: RunConfig, progress: ProgressSink) -> rusqlite::Result<BackfillCounts> {
    let cache = RawCache::new(&config.raw_dir);
    let mut conn = storage::connect(&config.db_file)?;
    storage::init_db(&conn)?;

    let pages = storage::load_backfill_pages(&conn)?;
    progress.header(&format!(
        "Backfilling links from {} stored HTML pages",
        pages.len()
    ));

    // One timestamp for the whole pass: every edge this run first records shares a
    // single `first_seen_at`, which is exactly what it means.
    let now = now_iso();
    let mut counts = BackfillCounts::default();
    let mut tx = conn.transaction()?;
    for (i, page) in pages.iter().enumerate() {
        let path = cache.path_for(&page.content_sha256, ext_for("html"));
        let data = match std::fs::read(&path) {
            Ok(d) => d,
            Err(_) => {
                counts.raw_missing += 1;
                continue;
            }
        };
        counts.pages += 1;
        let html = String::from_utf8_lossy(&data);
        let depth = page.depth + 1;
        let edges: Vec<LinkEdge> = discover_all_links(&html, &page.url)
            .into_iter()
            .map(|dst| {
                let ind = in_domain(&dst, &page.site);
                LinkEdge {
                    src: page.url.clone(),
                    dst,
                    site: page.site.clone(),
                    in_domain: ind,
                    depth,
                    first_seen_at: now.clone(),
                }
            })
            .collect();

        // total_changes() is monotonic per connection, so the delta across the
        // INSERT OR IGNORE is the number of edges that were actually new.
        let before = tx.total_changes();
        storage::insert_links(&tx, &edges)?;
        counts.edges += (tx.total_changes() - before) as i64;

        if (i + 1) % COMMIT_EVERY == 0 {
            tx.commit()?;
            tx = conn.transaction()?;
        }
    }
    tx.commit()?;
    storage::checkpoint_truncate(&conn);
    progress.summary("links", &counts.pairs());
    Ok(counts)
}
