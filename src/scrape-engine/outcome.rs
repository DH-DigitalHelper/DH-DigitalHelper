//! Per-page crawl outcomes and the content-hash change-detection decision.
//!
//! Mirrors the branching in the Python `crawl.process_url`. The full branch
//! (304 / non-2xx / classify=="other" / 2xx html|pdf) lives in `crawl.rs`; the
//! pure content-hash decision for the 2xx html|pdf case is here so it can be
//! unit-tested in isolation.

/// The seven terminal outcomes of processing one URL. String forms match the
/// keys used by `crawl_log.outcome`, the per-site counts dict, and the tests.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    New,
    Changed,
    Unchanged,
    Removed,
    Error,
    Skipped,
}

impl Outcome {
    pub fn as_str(self) -> &'static str {
        match self {
            Outcome::New => "new",
            Outcome::Changed => "changed",
            Outcome::Unchanged => "unchanged",
            Outcome::Removed => "removed",
            Outcome::Error => "error",
            Outcome::Skipped => "skipped",
        }
    }
}

/// Decide the outcome of a successfully fetched html|pdf body against the URL's
/// stored state. Returns `(outcome, changed)` where `changed` gates writing the
/// raw file + upserting `raw_docs` (exactly as in `process_url`):
///
/// ```text
/// changed = new_digest != prior_sha  OR  not present
/// outcome = New       if prior_sha is None
///           Changed   if changed
///           Unchanged otherwise
/// ```
pub fn content_outcome(
    prior_sha: Option<&str>,
    present: bool,
    new_digest: &str,
) -> (Outcome, bool) {
    let changed = prior_sha != Some(new_digest) || !present;
    let outcome = if prior_sha.is_none() {
        Outcome::New
    } else if changed {
        Outcome::Changed
    } else {
        Outcome::Unchanged
    };
    (outcome, changed)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn brand_new_url_is_new_and_changed() {
        let (o, changed) = content_outcome(None, true, "aaa");
        assert_eq!(o, Outcome::New);
        assert!(changed);
    }

    #[test]
    fn same_hash_present_is_unchanged() {
        let (o, changed) = content_outcome(Some("aaa"), true, "aaa");
        assert_eq!(o, Outcome::Unchanged);
        assert!(!changed);
    }

    #[test]
    fn different_hash_is_changed() {
        let (o, changed) = content_outcome(Some("aaa"), true, "bbb");
        assert_eq!(o, Outcome::Changed);
        assert!(changed);
    }

    #[test]
    fn same_hash_but_absent_is_changed_resurrection() {
        // A previously-removed URL (present=0) that now returns identical bytes
        // must re-trigger extraction, so it counts as changed even on a hash match.
        let (o, changed) = content_outcome(Some("aaa"), false, "aaa");
        assert_eq!(o, Outcome::Changed);
        assert!(changed);
    }
}
