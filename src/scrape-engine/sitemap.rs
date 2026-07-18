//! Best-effort sitemap discovery: (url, lastmod) pairs, in-domain only.

use std::collections::{HashMap, HashSet};
use std::io::Read;
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

/// Discover in-domain URLs from a site's sitemap, following in-domain nested sitemap-index levels.
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
    if let Some(bytes) = client
        .fetch_bytes(format!("{homepage}/robots.txt"), user_agent.to_string())
        .await
    {
        for loc in robots_sitemaps(&String::from_utf8_lossy(&bytes)) {
            if in_domain(&loc, allowed_domain) {
                to_visit.push(loc);
            }
        }
    }
    let mut visited: HashSet<String> = HashSet::new();
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
        let xml = decode_xml(&bytes);
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

/// Decode a fetched sitemap body to XML text, inflating it first if it is a gzip member.
fn decode_xml(bytes: &[u8]) -> String {
    if bytes.starts_with(&[0x1f, 0x8b]) {
        let mut out = String::new();
        if flate2::read::GzDecoder::new(bytes)
            .read_to_string(&mut out)
            .is_ok()
        {
            return out;
        }
    }
    String::from_utf8_lossy(bytes).into_owned()
}

/// The targets of robots.txt `Sitemap:` lines.
fn robots_sitemaps(text: &str) -> Vec<String> {
    text.lines()
        .filter_map(|line| {
            let (key, value) = line.split_once(':')?;
            if !key.trim().eq_ignore_ascii_case("sitemap") {
                return None;
            }
            let value = value.trim();
            (!value.is_empty()).then(|| value.to_string())
        })
        .collect()
}

/// Unescape the XML entities that appear in `<loc>`/`<lastmod>` text.
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
    fn robots_sitemaps_are_case_insensitive_and_ignore_other_directives() {
        let robots = "User-agent: *\n\
                      Disallow: /admin\n\
                      Sitemap: https://a.de/sitemap.xml\n\
                      sitemap:  https://a.de/news.xml  \n\
                      SITEMAP: https://a.de/shouty.xml\n\
                      Crawl-delay: 5\n\
                      Sitemap:\n";
        assert_eq!(
            robots_sitemaps(robots),
            vec![
                "https://a.de/sitemap.xml".to_string(),
                "https://a.de/news.xml".to_string(),
                "https://a.de/shouty.xml".to_string(),
            ],
            "an empty Sitemap: yields nothing; other directives are ignored"
        );
    }

    #[test]
    fn decode_xml_inflates_a_gzip_member_and_passes_plain_text_through() {
        use flate2::Compression;
        use flate2::write::GzEncoder;
        use std::io::Write;

        let plain = "<urlset><url><loc>https://a.de/x</loc></url></urlset>";
        let mut enc = GzEncoder::new(Vec::new(), Compression::default());
        enc.write_all(plain.as_bytes()).unwrap();
        let gz = enc.finish().unwrap();

        assert_eq!(decode_xml(&gz), plain, "gzip member must be inflated");
        assert_eq!(
            decode_xml(plain.as_bytes()),
            plain,
            "plain XML passes through"
        );
    }

    #[test]
    fn unescape_handles_amp_and_numeric() {
        assert_eq!(unescape("a=1&amp;b=2"), "a=1&b=2");
        assert_eq!(unescape("x&#38;y"), "x&y");
        assert_eq!(unescape("plain"), "plain");
    }
}
