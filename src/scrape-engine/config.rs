//! Run configuration passed from Python.
//!
//! `config.py` parses `config.toml` into a frozen `Config` dataclass and the thin
//! `crawl.run_fetch` adapter flattens it into a plain dict, so Rust never re-reads
//! `config.toml`. There are no CLI overrides: what the file says is what arrives
//! here.
//!
//! Every field is mandatory (`from_item_all` extracts each by dict key, so a
//! missing one is an extraction error rather than a default). `config.py` owns both
//! the defaulting and the range checks.

use pyo3::prelude::*;

#[derive(FromPyObject, Clone, Debug)]
#[pyo3(from_item_all)]
pub struct SiteCfg {
    pub name: String,
    pub seed_url: String,
    pub allowed_domain: String,
}

#[derive(FromPyObject, Clone, Debug)]
#[pyo3(from_item_all)]
pub struct RunConfig {
    pub sites: Vec<SiteCfg>,
    pub use_sitemap: bool,
    pub max_pages: i64,
    pub max_pages_per_host: i64,
    pub request_delay_seconds: f64,
    pub workers_per_host: i64,
    pub recheck: String,
    pub user_agent: String,
    pub db_file: String,
    pub raw_dir: String,
}

impl RunConfig {
    /// Number of concurrent fetch workers per host, floored at 1 (mirrors
    /// `max(1, config.crawl.workers_per_host)` in the old `run_fetch`).
    pub fn workers_per_host(&self) -> usize {
        self.workers_per_host.max(1) as usize
    }

    /// `recheck == "new-only"`: only fetch queued URLs never fetched before.
    pub fn only_new(&self) -> bool {
        self.recheck == "new-only"
    }

    /// Re-queue every already-present URL for this run. Both `"all"` and
    /// `"force-full"` do; the latter is `"all"` plus dropped validators.
    pub fn rechecks_all(&self) -> bool {
        matches!(self.recheck.as_str(), "all" | "force-full")
    }

    /// `recheck == "force-full"`: send no stored `ETag`/`Last-Modified`, so every
    /// re-checked URL is downloaded in full instead of revalidating to a cheap 304.
    ///
    /// Derived from `recheck` rather than carried beside it: as a separate flag it
    /// could be combined with `"new-only"` to ask for a forced re-download of pages
    /// that by definition have nothing stored to re-download. That is now
    /// unrepresentable.
    pub fn force_full(&self) -> bool {
        self.recheck == "force-full"
    }
}
