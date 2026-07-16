//! Run configuration passed from Python.
//!
//! The Python side (`config.py` + `cli.py`) already parses `config.toml` and
//! applies any CLI overrides (`--max-pages`, `--workers-per-host`,
//! `--new-only`/`--changed-only`/`--full`) onto the frozen `Config` dataclass.
//! The thin `crawl.run_fetch` adapter then hands us a plain dict with the final
//! values, so Rust never re-reads `config.toml`.

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
}
