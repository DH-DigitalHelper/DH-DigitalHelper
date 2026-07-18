//! Phase-1 crawler for `dhbw-scraper`, implemented in Rust and exposed to Python as the `scraper._engine` extension module.

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
#[pyfunction]
#[pyo3(signature = (config, run_id, progress=None))]
fn run_fetch<'py>(
    py: Python<'py>,
    config: config::RunConfig,
    run_id: String,
    progress: Option<Py<PyAny>>,
) -> PyResult<Bound<'py, PyDict>> {
    let sink = progress::ProgressSink::new(progress);
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
