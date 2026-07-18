import types

from scraper import dashboard
from scraper import storage as st

NOW = "2026-07-16T00:00:00"


def _db(tmp_path):
    db_file = tmp_path / "db.sqlite3"
    conn = st.connect(str(db_file))
    st.init_db(conn)
    return conn, db_file


def _sites():
    return [types.SimpleNamespace(name="Alpha", allowed_domain="alpha.de")]


def _doc(conn, url, *, site="alpha.de", words=120):
    conn.execute(
        "INSERT INTO documents (id, url, site, source_type, content_sha256, text, "
        "markdown, word_count, first_indexed_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (url, url, site, "html", "sha-" + url, "body", "body", words, NOW, NOW),
    )


def _uid(conn, url):
    """Intern a URL into the urls dictionary and return its id."""
    conn.execute("INSERT OR IGNORE INTO urls(url) VALUES (?)", (url,))
    return conn.execute("SELECT id FROM urls WHERE url=?", (url,)).fetchone()[0]


def _link(conn, src, dst, *, in_domain, site="alpha.de", depth=1):
    conn.execute(
        "INSERT OR IGNORE INTO links (src_id, dst_id, site, in_domain, depth, first_seen_at) "
        "VALUES (?,?,?,?,?,?)",
        (_uid(conn, src), _uid(conn, dst), site, 1 if in_domain else 0, depth, NOW),
    )


def _queue(conn, url, *, discovered_from=None, state="done", site="alpha.de", depth=0):
    conn.execute(
        "INSERT INTO queue (url, site, depth, discovered_from, work_state, first_seen_at) "
        "VALUES (?,?,?,?,?,?)",
        (url, site, depth, discovered_from, state, NOW),
    )


def test_scraped_content_cannot_break_out_of_the_json_island(tmp_path):
    """Scraped content with a raw </script> must not break out of the JSON script island in either rendered page."""
    conn, db_file = _db(tmp_path)
    _doc(conn, "https://www.alpha.de/a")
    payload = "</script><img src=x onerror=alert(1)>"
    conn.execute(
        "INSERT INTO raw_docs (content_sha256, source_type, raw_path, bytes, "
        "first_seen_at, extract_state, extract_error) VALUES (?,?,?,?,?,?,?)",
        ("sha-bad", "html", "/raw/bad.html", 10, NOW, "error", payload),
    )
    _queue(conn, "https://www.alpha.de/" + payload, state="done")
    conn.commit()

    data = dashboard.collect_analysis(
        conn, sites=_sites(), min_words=50, db_path=db_file
    )

    for html in (dashboard.render_html(data), dashboard.render_graph_html(data)):
        assert payload not in html, "the raw </script> payload reached the document"
        assert html.count("<script") == html.count("</script>")
    assert "onerror" in dashboard.render_html(data)


def test_top_external_targets_tally_counts_only_unfollowed_hosts(tmp_path):
    conn, db_file = _db(tmp_path)
    _doc(conn, "https://www.alpha.de/a")
    _doc(conn, "https://www.alpha.de/b")
    _link(conn, "https://www.alpha.de/a", "https://www.alpha.de/b", in_domain=True)
    _link(conn, "https://www.alpha.de/a", "https://moodle.alpha.de/x", in_domain=True)
    _link(conn, "https://www.alpha.de/a", "https://github.com/x", in_domain=False)
    _link(conn, "https://www.alpha.de/b", "https://github.com/y", in_domain=False)
    conn.commit()

    data = dashboard.collect_analysis(
        conn, sites=_sites(), min_words=50, db_path=db_file
    )
    ext = {x["host"]: x["n"] for x in data["links"]["top_external"]}
    assert ext == {"github.com": 2}


def test_report_links_to_graph_page_without_inlining_the_tree(tmp_path):
    conn, db_file = _db(tmp_path)
    _queue(conn, "https://www.alpha.de/", state="done")
    conn.commit()

    data = dashboard.collect_analysis(
        conn, sites=_sites(), min_words=50, db_path=db_file
    )
    assert data["discovery"]["sites"], "expected one campus tree"
    html = dashboard.render_html(data, graph_href="discovery.html")
    assert 'href="discovery.html"' in html
    assert 'class="disctree"' not in html
    assert "d3.hierarchy" not in html
    assert html.count("<script") == 1


def test_graph_page_has_tree_and_inlined_d3(tmp_path):
    conn, db_file = _db(tmp_path)
    _queue(conn, "https://www.alpha.de/", state="done")
    _queue(
        conn,
        "https://www.alpha.de/studium",
        discovered_from="https://www.alpha.de/",
        state="done",
    )
    conn.commit()

    data = dashboard.collect_analysis(
        conn, sites=_sites(), min_words=50, db_path=db_file
    )
    graph = dashboard.render_graph_html(data, report_href="analysis.html")
    assert 'id="disc-site"' in graph
    assert 'class="disctree"' in graph
    assert 'id="report-data"' in graph
    assert "d3.hierarchy" in graph
    assert 'href="analysis.html"' in graph
    assert "Alpha" in graph
    assert len(graph) > 100_000


def test_write_report_emits_report_and_graph_pages(tmp_path):
    conn, db_file = _db(tmp_path)
    _queue(conn, "https://www.alpha.de/", state="done")
    _queue(
        conn,
        "https://www.alpha.de/studium",
        discovered_from="https://www.alpha.de/",
        state="done",
    )
    conn.commit()

    out = tmp_path / "analysis.html"
    report_path, graph_path = dashboard.write_report(
        conn, sites=_sites(), min_words=50, db_path=db_file, out_path=out
    )
    assert report_path == out and report_path.exists()
    assert graph_path == out.with_name("discovery.html") and graph_path.exists()

    report = report_path.read_text(encoding="utf-8")
    graph = graph_path.read_text(encoding="utf-8")
    assert 'href="discovery.html"' in report
    assert 'class="disctree"' not in report
    assert 'class="disctree"' in graph
    assert 'href="analysis.html"' in graph


def test_empty_queue_discovery_degrades_to_warnbox(tmp_path):
    conn, db_file = _db(tmp_path)
    _doc(conn, "https://www.alpha.de/a")
    conn.commit()

    data = dashboard.collect_analysis(
        conn, sites=_sites(), min_words=50, db_path=db_file
    )
    assert data["discovery"]["sites"] == []
    report = dashboard.render_html(data)
    graph = dashboard.render_graph_html(data)
    assert 'href="discovery.html"' not in report
    assert "No crawled URLs yet" in report
    assert 'class="disctree"' not in graph
    assert "No crawled URLs yet" in graph


def _by_u(nodes):
    return {n["u"]: n for n in nodes}


def test_discovery_tree_builds_rooted_parent_child_structure():
    rows = [
        ("https://www.alpha.de/", "alpha.de", None, 0, "done"),
        (
            "https://www.alpha.de/studium",
            "alpha.de",
            "https://www.alpha.de/",
            1,
            "done",
        ),
        (
            "https://www.alpha.de/studium/bachelor",
            "alpha.de",
            "https://www.alpha.de/studium",
            2,
            "done",
        ),
    ]
    out = dashboard._discovery_trees(rows, _sites())
    (site,) = out["sites"]
    assert site["name"] == "Alpha"
    assert site["host"] == "www.alpha.de"

    nodes = site["nodes"]
    assert nodes[0]["st"] == "root" and nodes[0]["p"] == -1
    by_u = _by_u(nodes)
    assert by_u["/"]["p"] == 0
    assert by_u["/studium"]["p"] == nodes.index(by_u["/"])
    assert by_u["/studium/bachelor"]["p"] == nodes.index(by_u["/studium"])
    for i, n in enumerate(nodes):
        assert n["p"] < i


def test_discovery_tree_prunes_error_dead_end_leaves():
    rows = [
        ("https://www.alpha.de/", "alpha.de", None, 0, "done"),
        ("https://www.alpha.de/ok", "alpha.de", "https://www.alpha.de/", 1, "done"),
        (
            "https://www.alpha.de/broken",
            "alpha.de",
            "https://www.alpha.de/",
            1,
            "error",
        ),
    ]
    out = dashboard._discovery_trees(rows, _sites())
    us = _by_u(out["sites"][0]["nodes"])
    assert "/ok" in us
    assert "/broken" not in us


def test_discovery_tree_caps_children_with_more_marker():
    rows = [("https://www.alpha.de/", "alpha.de", None, 0, "done")]
    for i in range(5):
        rows.append(
            (
                f"https://www.alpha.de/c{i}",
                "alpha.de",
                "https://www.alpha.de/",
                1,
                "done",
            )
        )
    out = dashboard._discovery_trees(rows, _sites(), max_children=2)
    nodes = out["sites"][0]["nodes"]
    seed_idx = nodes.index(_by_u(nodes)["/"])
    kids = [n for n in nodes if n["p"] == seed_idx]
    real = [n for n in kids if n["st"] != "more"]
    more = [n for n in kids if n["st"] == "more"]
    assert len(real) == 2
    assert len(more) == 1 and more[0]["u"].startswith("+") and "more" in more[0]["u"]


def test_discovery_tree_caps_total_nodes_and_flags_truncated():
    rows = [("https://www.alpha.de/", "alpha.de", None, 0, "done")]
    for i in range(20):
        rows.append(
            (
                f"https://www.alpha.de/p{i}",
                "alpha.de",
                "https://www.alpha.de/",
                1,
                "done",
            )
        )
    out = dashboard._discovery_trees(rows, _sites(), max_nodes=5)
    site = out["sites"][0]
    assert site["truncated"] is True
    assert len(site["nodes"]) <= 5
    assert site["total"] == 21
    for i, n in enumerate(site["nodes"]):
        assert n["p"] < i


def test_discovery_tree_groups_by_site_in_config_order_and_picks_default():
    rows = [
        ("https://www.beta.de/", "beta.de", None, 0, "done"),
        ("https://www.alpha.de/", "alpha.de", None, 0, "done"),
    ]
    sites = [
        types.SimpleNamespace(name="Portal", allowed_domain="www.dhbw.de"),
        types.SimpleNamespace(name="Alpha", allowed_domain="alpha.de"),
        types.SimpleNamespace(name="Beta", allowed_domain="beta.de"),
    ]
    rows.append(("https://www.dhbw.de/", "www.dhbw.de", None, 0, "done"))
    out = dashboard._discovery_trees(rows, sites)
    assert [s["name"] for s in out["sites"]] == ["Portal", "Alpha", "Beta"]
    assert out["sites"][out["default"]]["name"] == "Portal"
