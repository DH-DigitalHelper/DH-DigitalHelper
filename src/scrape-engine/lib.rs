//! Phase-1 crawler for `dhbw-scraper`, implemented in Rust and exposed to Python
//! as the `scraper._engine` extension module.
//!
//! The single public entry point is [`run_fetch`], a drop-in replacement for the
//! former Python `crawl.run_fetch`. It owns the whole crawl: async fetching,
//! link/sitemap discovery, the in-memory frontier, and all Phase-1 SQLite writes
//! through one dedicated writer task. Phase 2 (extraction) stays in Python and
//! reads the same SQLite database and content-addressed `data/raw/` cache.

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

pub mod config;
pub mod crawl;
pub mod fetch;
pub mod links;
pub mod outcome;
pub mod progress;
pub mod sitemap;
pub mod storage;
pub mod writer;

/// Run the entire Phase-1 crawl.
///
/// `config` is the plain dict the Python `crawl.run_fetch` adapter builds from the
/// parsed `Config`. Everything the crawl needs is in it — including whether to drop
/// stored validators, which is derived from `recheck == "force-full"`. `progress`,
/// if given, is the existing Python `Progress` instance; Rust calls back into it
/// (throttled) during the crawl. Returns `{site_name: {counts...}}`.
#[pyfunction]
#[pyo3(signature = (config, run_id, progress=None))]
fn run_fetch<'py>(
    py: Python<'py>,
    config: config::RunConfig,
    run_id: String,
    progress: Option<Py<PyAny>>,
) -> PyResult<Bound<'py, PyDict>> {
    let sink = progress::ProgressSink::new(progress);
    // Detach from the interpreter (release the GIL) for the whole crawl; the
    // coordinator thread and the progress bridge re-attach only briefly when
    // calling back into Python.
    let counts = py
        .detach(move || crawl::run(config, run_id, sink))
        .map_err(|e| PyRuntimeError::new_err(e.to_string()))?;

    let out = PyDict::new(py);
    for (name, c) in counts {
        let d = PyDict::new(py);
        for (k, v) in c.pairs() {
            d.set_item(k, v)?;
        }
        out.set_item(name, d)?;
    }
    Ok(out)
}

#[pymodule]
fn _engine(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(run_fetch, m)?)?;
    Ok(())
}
