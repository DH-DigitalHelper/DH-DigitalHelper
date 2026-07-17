//! Link discovery from HTML + in-domain filtering + crawler-trap detection.
//!
//! A verbatim port of the Python `links.py`. The trap constants MUST stay in
//! exact sync with the originals — they are load-bearing crawl-shaping rules
//! (see the parity tests in `tests/links_parity.rs`).
//!
//! Discovery is split into two stages so the new `links` edge table can record
//! the *full* outbound set while following stays strictly in-domain:
//!   * [`discover_all_links`] — every `<a href>` target, absolutized + deduped,
//!     regardless of domain or trap status (feeds the edge table).
//!   * [`followable`] — the in-domain, non-trap subset (feeds the queue).

use std::collections::HashSet;
use url::Url;

// Exact hosts (or any subdomain of them) that are entire webapps with no
// indexable content. Matched like `in_domain`. Currently empty: the MRBS booking
// trap that used to be pinned here (buchen.dhbw-vs.de) is now caught more broadly
// by the leftmost-label rule below, so every campus's booking host is covered
// without pinning each one. Kept as a seam for future exact-host traps.
const TRAP_HOSTS: &[&str] = &[];

// Whole-host traps matched by the leftmost DNS label, so each per-campus instance
// is covered without listing every subdomain:
//   * "buchen"  — Meeting Room Booking System (MRBS): every request is a fresh
//     ?year=&month=&day=&area=&room=... permutation. buchen.dhbw-vs.de alone
//     exploded to ~940k distinct URLs in one run; any campus's "buchen.*" host is
//     the same booking webapp.
//   * "moodle" / "elearning" — login-walled LMS instances (moodle2., moodle27., ...).
// Matched on the host label only, so a normal page whose *path* contains the word
// (e.g. www.mosbach.dhbw.de/.../campus-buchen) is unaffected.
const TRAP_HOST_LABEL_PREFIXES: &[&str] = &["buchen", "moodle", "elearning"];

// Path fragments identifying infinite spider traps (Moodle's calendar walks
// ?time= toward +/-infinity). Matched on the URL path only.
const TRAP_PATH_FRAGMENTS: &[&str] = &["/calendar/view.php"];

// Query-string key prefixes marking duplicate/permutation URLs of a clean page
// we already crawl at its canonical (param-free) URL. Matched as a prefix of the
// decoded, lowercased key.
const TRAP_QUERY_KEY_PREFIXES: &[&str] = &[
    "replytocom",
    "forcedownload",
    "tx_solr",
    "tx_dhbwcontent[accordioncontainer]",
];

/// True if `url`'s host is `allowed_domain` or a subdomain of it (case-insensitive).
/// A URL that fails to parse or has no host is not in-domain.
pub fn in_domain(url: &str, allowed_domain: &str) -> bool {
    match Url::parse(url) {
        Ok(u) => match u.host_str() {
            Some(host) => host_in_domain(host, allowed_domain),
            None => false,
        },
        Err(_) => false,
    }
}

fn host_in_domain(host: &str, allowed_domain: &str) -> bool {
    let host = host.to_ascii_lowercase();
    let allowed = allowed_domain.to_ascii_lowercase();
    host == allowed || host.ends_with(&format!(".{allowed}"))
}

fn host_is_trap(host: &str) -> bool {
    let host = host.to_ascii_lowercase();
    if TRAP_HOSTS
        .iter()
        .any(|h| host == *h || host.ends_with(&format!(".{h}")))
    {
        return true;
    }
    let label = host.split_once('.').map(|(l, _)| l).unwrap_or(&host);
    TRAP_HOST_LABEL_PREFIXES
        .iter()
        .any(|p| label.starts_with(p))
}

/// True if the URL is a known crawler trap that must never be enqueued.
pub fn is_trap_url(url: &str) -> bool {
    let parsed = match Url::parse(url) {
        Ok(u) => u,
        Err(_) => return false,
    };
    if let Some(host) = parsed.host_str()
        && host_is_trap(host)
    {
        return true;
    }
    let path = parsed.path().to_ascii_lowercase();
    if TRAP_PATH_FRAGMENTS.iter().any(|frag| path.contains(frag)) {
        return true;
    }
    // Decoded, lowercased query keys (keep_blank_values=True equivalent: pairs()
    // yields a key even when the value is empty).
    parsed.query_pairs().any(|(k, _)| {
        let k = k.to_ascii_lowercase();
        TRAP_QUERY_KEY_PREFIXES.iter().any(|p| k.starts_with(p))
    })
}

/// The value of the `name` attribute, matched case-insensitively as HTML requires.
/// `tl`'s own `get()` compares the raw bytes, so it misses `HREF` / `Href`.
fn attr_ignore_case<'a>(
    attrs: &'a tl::Attributes<'a>,
    name: &str,
) -> Option<std::borrow::Cow<'a, str>> {
    attrs
        .iter()
        .find(|(key, _)| key.eq_ignore_ascii_case(name))
        .and_then(|(_, value)| value)
}

/// Every `<a href>` target on the page as an absolute, fragment-stripped http(s)
/// URL, deduped in first-seen order. Drops `mailto:`/`tel:`/`javascript:` and any
/// href that fails to resolve to an http(s) URL. No domain/trap filtering — this
/// is the full outbound set for the edge table.
pub fn discover_all_links(html: &str, base_url: &str) -> Vec<String> {
    let base = match Url::parse(base_url) {
        Ok(b) => b,
        Err(_) => return Vec::new(),
    };
    let dom = match tl::parse(html, tl::ParserOptions::default()) {
        Ok(d) => d,
        Err(_) => return Vec::new(),
    };
    let mut out: Vec<String> = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    for node in dom.nodes() {
        let Some(tag) = node.as_tag() else { continue };
        // Tag and attribute names are case-insensitive in HTML, but `tl` returns
        // them exactly as written and folds nothing -- so comparing literally
        // silently dropped `<A>` / `HREF=` anchors, losing both the edge and the
        // follow target.
        if !tag.name().as_utf8_str().eq_ignore_ascii_case("a") {
            continue;
        }
        let href = match attr_ignore_case(tag.attributes(), "href") {
            Some(value) => value,
            None => continue,
        };
        let href = href.trim();
        if href.is_empty() {
            continue;
        }
        let lower = href.to_ascii_lowercase();
        if lower.starts_with("mailto:")
            || lower.starts_with("tel:")
            || lower.starts_with("javascript:")
        {
            continue;
        }
        let mut abs = match base.join(href) {
            Ok(u) => u,
            Err(_) => continue,
        };
        abs.set_fragment(None);
        let scheme = abs.scheme();
        if scheme != "http" && scheme != "https" {
            continue;
        }
        let s = abs.to_string();
        if seen.insert(s.clone()) {
            out.push(s);
        }
    }
    out
}

/// The in-domain, non-trap subset of `all` — the URLs the crawler will follow.
pub fn followable<'a>(all: &'a [String], allowed_domain: &str) -> Vec<&'a String> {
    all.iter()
        .filter(|u| in_domain(u, allowed_domain) && !is_trap_url(u))
        .collect()
}

/// Faithful port of Python `discover_links`: in-domain, non-trap, deduped links.
/// Kept for parity testing against `test_links.py`; production code uses the
/// two-stage [`discover_all_links`] + [`followable`].
pub fn discover_links(html: &str, base_url: &str, allowed_domain: &str) -> Vec<String> {
    let all = discover_all_links(html, base_url);
    followable(&all, allowed_domain)
        .into_iter()
        .cloned()
        .collect()
}
