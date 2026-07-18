//! Bridge from the coordinator thread to the Python `Progress` renderer.

use pyo3::prelude::*;
use pyo3::types::PyDict;

/// One site's snapshot to hand to `Progress.update`.
pub struct SiteUpdate {
    pub key: String,
    pub counts: [(&'static str, i64); 7],
    pub current: String,
    pub queued: i64,
}

/// Owns the optional Python `Progress` instance.
pub struct ProgressSink {
    obj: Option<Py<PyAny>>,
}

impl ProgressSink {
    pub fn new(obj: Option<Py<PyAny>>) -> Self {
        Self { obj }
    }

    /// A second handle to the same Python object (new reference under the GIL), so the coordinator thread and the orchestrator can both call it.
    pub fn try_clone(&self) -> Self {
        Self {
            obj: self
                .obj
                .as_ref()
                .map(|o| Python::attach(|py| o.clone_ref(py))),
        }
    }

    pub fn header(&self, text: &str) {
        let Some(obj) = &self.obj else { return };
        Python::attach(|py| {
            let _ = obj.bind(py).call_method1("header", (text,));
        });
    }

    pub fn update(&self, updates: &[SiteUpdate]) {
        let Some(obj) = &self.obj else { return };
        Python::attach(|py| {
            let bound = obj.bind(py);
            for u in updates {
                let counts = PyDict::new(py);
                for (k, v) in u.counts {
                    let _ = counts.set_item(k, v);
                }
                let kwargs = PyDict::new(py);
                let _ = kwargs.set_item("key", &u.key);
                let _ = kwargs.set_item("queued", u.queued);
                let _ = bound.call_method("update", (counts, &u.current), Some(&kwargs));
            }
        });
    }

    pub fn summary(&self, title: &str, counts: &[(&'static str, i64)]) {
        let Some(obj) = &self.obj else { return };
        Python::attach(|py| {
            let d = PyDict::new(py);
            for (k, v) in counts {
                let _ = d.set_item(k, v);
            }
            let _ = obj.bind(py).call_method1("summary", (title, d));
        });
    }
}
