//! Per-page crawl outcomes and the content-hash change-detection decision.

/// The seven terminal outcomes of processing one URL.
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

/// Decide the outcome of a successfully fetched html|pdf body against the URL's stored state.
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
        let (o, changed) = content_outcome(Some("aaa"), false, "aaa");
        assert_eq!(o, Outcome::Changed);
        assert!(changed);
    }
}
