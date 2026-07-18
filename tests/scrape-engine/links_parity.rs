//! Verbatim port of the Python `tests/test_links.py`, locking the trap rules and link-discovery behaviour to the originals.

use _engine::links::{discover_links, in_domain, is_trap_url};

fn has(v: &[String], s: &str) -> bool {
    v.iter().any(|u| u == s)
}

#[test]
fn in_domain_matches_host_and_subdomains() {
    assert!(in_domain("https://www.dhbw.de/x", "www.dhbw.de"));
    assert!(in_domain("https://sub.www.dhbw.de/x", "www.dhbw.de"));
    assert!(!in_domain("https://mosbach.dhbw.de/x", "www.dhbw.de"));
}

#[test]
fn in_domain_case_insensitive() {
    assert!(in_domain("https://WWW.DHBW.DE/x", "WWW.DHBW.DE"));
    assert!(in_domain("https://www.dhbw.de/x", "WWW.DHBW.DE"));
    assert!(in_domain("https://WWW.DHBW.DE/x", "www.dhbw.de"));
    assert!(!in_domain("https://mosbach.dhbw.de/x", "WWW.DHBW.DE"));
}

#[test]
fn discover_links_filters_and_absolutizes() {
    let html = r#"
    <a href="/studium">rel</a>
    <a href="https://www.dhbw.de/kontakt#top">abs+frag</a>
    <a href="https://other.example/x">off-domain</a>
    <a href="mailto:a@b.de">mail</a>
    <a href="doc.pdf">pdf</a>
    "#;
    let got = discover_links(html, "https://www.dhbw.de/home", "www.dhbw.de");
    assert!(has(&got, "https://www.dhbw.de/studium"));
    assert!(has(&got, "https://www.dhbw.de/kontakt"));
    assert!(has(&got, "https://www.dhbw.de/doc.pdf"));
    assert!(got.iter().all(|u| !u.contains("other.example")));
    assert!(got.iter().all(|u| !u.starts_with("mailto:")));
}

/// Link discovery is case-insensitive about tag and attribute names.
#[test]
fn discover_links_is_case_insensitive_about_tags_and_attributes() {
    let html = r#"
    <A href="/upper-tag">uppercase tag</A>
    <a HREF="/upper-attr">uppercase attr</a>
    <a Href="/mixed-attr">mixed attr</a>
    <A HREF="/both-upper">both</A>
    "#;
    let got = discover_links(html, "https://www.dhbw.de/home", "www.dhbw.de");
    for expected in [
        "https://www.dhbw.de/upper-tag",
        "https://www.dhbw.de/upper-attr",
        "https://www.dhbw.de/mixed-attr",
        "https://www.dhbw.de/both-upper",
    ] {
        assert!(has(&got, expected), "{expected} was dropped; got {got:?}");
    }
}

#[test]
fn discover_links_survives_malformed_html() {
    assert_eq!(
        discover_links("<a href=", "https://www.dhbw.de/", "www.dhbw.de"),
        Vec::<String>::new()
    );
}

#[test]
fn discover_links_survives_malformed_href() {
    let html = r#"<a href="http://[::1">bad</a><a href="/studium">good</a>"#;
    let got = discover_links(html, "https://www.dhbw.de/home", "www.dhbw.de");
    assert!(has(&got, "https://www.dhbw.de/studium"));
}

#[test]
fn is_trap_url_flags_moodle_calendar() {
    assert!(is_trap_url(
        "https://moodle.heidenheim.dhbw.de/calendar/view.php?view=month&time=13656204000"
    ));
    assert!(is_trap_url(
        "https://moodle.heidenheim.dhbw.de/calendar/view.php?view=month&time=-10121936008"
    ));
    assert!(is_trap_url(
        "https://moodle.heidenheim.dhbw.de/calendar/view.php?view=upcoming&course=976"
    ));
}

#[test]
fn is_trap_url_flags_mrbs_booking_host() {
    assert!(is_trap_url(
        "https://buchen.dhbw-vs.de/edit_entry.php?view=day&year=2026&month=7&day=14&area=5&room=10&hour=10&minute=30"
    ));
    assert!(is_trap_url(
        "https://buchen.dhbw-vs.de/index.php?view=week&area=5&room=10"
    ));
    assert!(is_trap_url("https://buchen.dhbw-vs.de/help.php"));
    assert!(is_trap_url("https://buchen.mosbach.dhbw.de/index.php"));
    assert!(is_trap_url(
        "https://buchen.dhbw-stuttgart.de/edit_entry.php?area=1"
    ));
}

#[test]
fn is_trap_url_allows_sibling_hosts_of_trap_host() {
    assert!(!is_trap_url("https://www.dhbw-vs.de/studium"));
    assert!(!is_trap_url("https://blog.dhbw-vs.de/?p=237"));
}

#[test]
fn is_trap_url_allows_buchen_in_path_on_normal_host() {
    assert!(!is_trap_url(
        "https://www.mosbach.dhbw.de/campus-mosbach/campus-buchen"
    ));
    assert!(!is_trap_url("https://www.mosbach.dhbw.de/standort/buchen"));
}

#[test]
fn is_trap_url_flags_lms_hosts() {
    assert!(is_trap_url(
        "https://moodle.dhbw-vs.de/mod/forum/discuss.php?d=86015"
    ));
    assert!(is_trap_url(
        "https://moodle.heidenheim.dhbw.de/course/view.php?id=976"
    ));
    assert!(is_trap_url(
        "https://moodle2.dhbw-loerrach.de/moodle/mod/data/edit.php?d=397"
    ));
    assert!(is_trap_url("https://moodle27.dhbw-stuttgart.de/"));
    assert!(is_trap_url(
        "https://elearning.dhbw-stuttgart.de/login/index.php"
    ));
    assert!(is_trap_url(
        "https://elearning.cas.dhbw.de/course/view.php?id=1"
    ));
}

#[test]
fn is_trap_url_allows_non_lms_hosts_mentioning_moodle() {
    assert!(!is_trap_url(
        "https://www.heilbronn.dhbw.de/studium/moodle/"
    ));
    assert!(!is_trap_url(
        "https://www.heilbronn.dhbw.de/informationen-fuer/dozierende/unsere-lernplattform-moodle/"
    ));
    assert!(!is_trap_url("https://www.dhbw-vs.de/studium"));
}

#[test]
fn is_trap_url_flags_solr_search_results() {
    assert!(is_trap_url("https://www.dhbw.de/suche?tx_solr%5Bpage%5D=3"));
    assert!(is_trap_url(
        "https://www.dhbw.de/suche?tx_solr%5Bfilter%5D%5B0%5D=type%3Apages"
    ));
}

#[test]
fn is_trap_url_flags_wordpress_reply_and_forcedownload() {
    assert!(is_trap_url("https://blog.dhbw-vs.de/?p=237&replytocom=5"));
    assert!(is_trap_url(
        "https://moodle.dhbw-vs.de/pluginfile.php/72070/mod_forum/attachment/26196/x.pdf?forcedownload=1"
    ));
}

#[test]
fn is_trap_url_flags_typo3_accordion_deeplinks() {
    assert!(is_trap_url(
        "https://www.dhbw.de/datenschutz?tx_dhbwcontent%5BaccordionContainer%5D=63385&tx_dhbwcontent%5BaccordionItem%5D=0"
    ));
}

#[test]
fn is_trap_url_allows_real_content() {
    assert!(!is_trap_url("https://www.dhbw.de/studium"));
    assert!(!is_trap_url("https://www.dhbw.de/doc.pdf"));
    assert!(!is_trap_url("https://www.dhbw.de/suche"));
    assert!(!is_trap_url("https://www.dhbw.de/datenschutz"));
}

#[test]
fn discover_links_drops_trap_urls() {
    let html = r#"
    <a href="/studium">keep</a>
    <a href="https://moodle.heidenheim.dhbw.de/calendar/view.php?view=month&time=99">trap</a>
    <a href="https://moodle.heidenheim.dhbw.de/course/view.php?id=5">lms-host trap</a>
    <a href="https://www.heidenheim.dhbw.de/kontakt">keep</a>
    "#;
    let got = discover_links(
        html,
        "https://www.heidenheim.dhbw.de/x",
        "heidenheim.dhbw.de",
    );
    assert!(has(&got, "https://www.heidenheim.dhbw.de/studium"));
    assert!(has(&got, "https://www.heidenheim.dhbw.de/kontakt"));
    assert!(got.iter().all(|u| !u.contains("moodle.heidenheim.dhbw.de")));
}

#[test]
fn discover_links_drops_booking_and_query_traps() {
    let html = r#"
    <a href="https://buchen.dhbw-vs.de/edit_entry.php?area=5&room=10">booking</a>
    <a href="https://www.dhbw-vs.de/suche?tx_solr%5Bpage%5D=3">search</a>
    <a href="https://www.dhbw-vs.de/studium">keep</a>
    "#;
    let got = discover_links(html, "https://www.dhbw-vs.de/x", "dhbw-vs.de");
    assert!(has(&got, "https://www.dhbw-vs.de/studium"));
    assert!(got.iter().all(|u| !u.contains("buchen.dhbw-vs.de")));
    assert!(got.iter().all(|u| !u.contains("tx_solr")));
}
