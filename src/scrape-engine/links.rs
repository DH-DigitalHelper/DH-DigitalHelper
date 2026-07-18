//! Link discovery from HTML + in-domain filtering + crawler-trap detection.

use std::collections::HashSet;
use url::Url;

const TRAP_HOSTS: &[&str] = &[];

const TRAP_HOST_LABEL_PREFIXES: &[&str] = &["buchen", "moodle", "elearning"];

const TRAP_PATH_FRAGMENTS: &[&str] = &["/calendar/view.php"];

const TRAP_QUERY_KEY_PREFIXES: &[&str] = &[
    "replytocom",
    "forcedownload",
    "tx_solr",
    "tx_dhbwcontent[accordioncontainer]",
];

/// True if `url`'s host is `allowed_domain` or a subdomain of it (case-insensitive).
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
    parsed.query_pairs().any(|(k, _)| {
        let k = k.to_ascii_lowercase();
        TRAP_QUERY_KEY_PREFIXES.iter().any(|p| k.starts_with(p))
    })
}

/// The value of the `name` attribute, matched case-insensitively as HTML requires.
fn attr_ignore_case<'a>(
    attrs: &'a tl::Attributes<'a>,
    name: &str,
) -> Option<std::borrow::Cow<'a, str>> {
    attrs
        .iter()
        .find(|(key, _)| key.eq_ignore_ascii_case(name))
        .and_then(|(_, value)| value)
}

/// Every `<a href>` target on the page as an absolute, fragment-stripped http(s) URL, deduped in first-seen order.
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
pub fn discover_links(html: &str, base_url: &str, allowed_domain: &str) -> Vec<String> {
    let all = discover_all_links(html, base_url);
    followable(&all, allowed_domain)
        .into_iter()
        .cloned()
        .collect()
}
