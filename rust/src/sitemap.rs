//! Best-effort sitemap discovery: (url, lastmod) pairs, in-domain only.
//!
//! Port of the Python `sitemap.py` (regex-based, tolerant). Follows nested
//! sitemap-index levels bounded by a `visited` set (cycle-safe), keeping only
//! in-domain locs and in-domain child sitemaps.

use std::collections::{HashMap, HashSet};
use std::sync::LazyLock;

use regex::Regex;
use url::Url;

use crate::fetch::HttpClient;
use crate::links::in_domain;

static LOC: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)<loc>\s*([^<\s]+)\s*</loc>").unwrap());
static URL_BLOCK: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?is)<url>(.*?)</url>").unwrap());
static SITEMAP_BLOCK: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?is)<sitemap>(.*?)</sitemap>").unwrap());
static LASTMOD: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)<lastmod>\s*([^<\s]+)\s*</lastmod>").unwrap());

/// Parse one sitemap document into `(url, lastmod)` pairs and child sitemap URLs.
pub fn parse(xml: &str) -> (Vec<(String, Option<String>)>, Vec<String>) {
    let mut url_pairs = Vec::new();
    for block in URL_BLOCK.captures_iter(xml) {
        let inner = &block[1];
        let Some(loc) = LOC.captures(inner) else {
            continue;
        };
        let lm = LASTMOD.captures(inner).map(|c| unescape(&c[1]));
        url_pairs.push((unescape(&loc[1]), lm));
    }
    let mut subs = Vec::new();
    for block in SITEMAP_BLOCK.captures_iter(xml) {
        if let Some(loc) = LOC.captures(&block[1]) {
            subs.push(unescape(&loc[1]));
        }
    }
    (url_pairs, subs)
}

/// Discover in-domain URLs from a site's sitemap, following in-domain nested
/// sitemap-index levels. Returns `(url, lastmod)` pairs. Never errors: an
/// unreachable/malformed sitemap degrades to whatever was found so far.
pub async fn discover<C: HttpClient>(
    seed_url: &str,
    allowed_domain: &str,
    client: &C,
    user_agent: &str,
) -> Vec<(String, Option<String>)> {
    let homepage = match Url::parse(seed_url) {
        Ok(u) => {
            let host = u.host_str().unwrap_or_default();
            match u.port() {
                Some(p) => format!("{}://{}:{}", u.scheme(), host, p),
                None => format!("{}://{}", u.scheme(), host),
            }
        }
        Err(_) => return Vec::new(),
    };

    let mut to_visit: Vec<String> = vec![format!("{homepage}/sitemap.xml")];
    let mut visited: HashSet<String> = HashSet::new();
    // Insertion-ordered accumulation of found urls (last lastmod wins, as in the
    // Python dict).
    let mut order: Vec<String> = Vec::new();
    let mut found: HashMap<String, Option<String>> = HashMap::new();

    while let Some(target) = to_visit.pop() {
        if !visited.insert(target.clone()) {
            continue;
        }
        let Some(bytes) = client
            .fetch_bytes(target.clone(), user_agent.to_string())
            .await
        else {
            continue;
        };
        let xml = String::from_utf8_lossy(&bytes);
        let (url_pairs, subs) = parse(&xml);
        for (url, lastmod) in url_pairs {
            if in_domain(&url, allowed_domain) {
                if !found.contains_key(&url) {
                    order.push(url.clone());
                }
                found.insert(url, lastmod);
            }
        }
        for sub in subs {
            if !visited.contains(&sub) && in_domain(&sub, allowed_domain) {
                to_visit.push(sub);
            }
        }
    }

    order
        .into_iter()
        .map(|u| {
            let lm = found.get(&u).cloned().flatten();
            (u, lm)
        })
        .collect()
}

/// Unescape the XML entities that appear in `<loc>`/`<lastmod>` text. Covers the
/// named entities plus numeric character references, matching Python's
/// `html.unescape` for the cases sitemaps use in practice.
fn unescape(s: &str) -> String {
    if !s.contains('&') {
        return s.to_string();
    }
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(amp) = rest.find('&') {
        out.push_str(&rest[..amp]);
        let after = &rest[amp..];
        if let Some(semi) = after.find(';') {
            let entity = &after[1..semi];
            let decoded = match entity {
                "amp" => Some('&'),
                "lt" => Some('<'),
                "gt" => Some('>'),
                "quot" => Some('"'),
                "apos" | "#39" => Some('\''),
                _ => decode_numeric(entity),
            };
            match decoded {
                Some(c) => {
                    out.push(c);
                    rest = &after[semi + 1..];
                    continue;
                }
                None => {
                    out.push('&');
                    rest = &after[1..];
                    continue;
                }
            }
        } else {
            out.push('&');
            rest = &after[1..];
        }
    }
    out.push_str(rest);
    out
}

fn decode_numeric(entity: &str) -> Option<char> {
    let num = entity.strip_prefix('#')?;
    let code = if let Some(hex) = num.strip_prefix(['x', 'X']) {
        u32::from_str_radix(hex, 16).ok()?
    } else {
        num.parse::<u32>().ok()?
    };
    char::from_u32(code)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_extracts_urls_lastmods_and_subs() {
        let xml = r#"<urlset>
          <url><loc>https://a.de/x</loc><lastmod>2026-02-01</lastmod></url>
          <url><loc>https://a.de/y</loc></url>
        </urlset>"#;
        let (pairs, subs) = parse(xml);
        assert_eq!(pairs.len(), 2);
        assert_eq!(
            pairs[0],
            ("https://a.de/x".into(), Some("2026-02-01".into()))
        );
        assert_eq!(pairs[1], ("https://a.de/y".into(), None));
        assert!(subs.is_empty());
    }

    #[test]
    fn parse_reads_sitemap_index() {
        let xml = r#"<sitemapindex>
          <sitemap><loc>https://a.de/sitemap-1.xml</loc></sitemap>
        </sitemapindex>"#;
        let (pairs, subs) = parse(xml);
        assert!(pairs.is_empty());
        assert_eq!(subs, vec!["https://a.de/sitemap-1.xml".to_string()]);
    }

    #[test]
    fn unescape_handles_amp_and_numeric() {
        assert_eq!(unescape("a=1&amp;b=2"), "a=1&b=2");
        assert_eq!(unescape("x&#38;y"), "x&y");
        assert_eq!(unescape("plain"), "plain");
    }
}
