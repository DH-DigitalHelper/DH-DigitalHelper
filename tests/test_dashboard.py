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
    """Intern a URL into the `urls` dictionary and return its id (mirrors the Rust
    writer's interner)."""
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
    """The whole analysis dict is json.dumps()'d straight into a <script> island.

    json.dumps escapes quotes and control characters but NOT `</script>`, and the
    dict carries free-text straight from the DB -- extractor errors AND now crawled
    URLs feeding the discovery tree -- quoting whatever a scraped page contained.
    One raw `</script>` closes the island early and the rest becomes live DOM in the
    operator's browser. render_html neutralises `</` -> `<\\/`; assert nothing breaks
    out through either vector.
    """
    conn, db_file = _db(tmp_path)
    _doc(conn, "https://www.alpha.de/a")
    payload = "</script><img src=x onerror=alert(1)>"
    conn.execute(
        "INSERT INTO raw_docs (content_sha256, source_type, raw_path, bytes, "
        "first_seen_at, extract_state, extract_error) VALUES (?,?,?,?,?,?,?)",
        ("sha-bad", "html", "/raw/bad.html", 10, NOW, "error", payload),
    )
    # a crawled URL whose path carries the same breakout string feeds the tree.
    _queue(conn, "https://www.alpha.de/" + payload, state="done")
    conn.commit()

    data = dashboard.collect_analysis(
        conn, sites=_sites(), min_words=50, db_path=db_file
    )

    # Both pages inline DB free text into a <script> island: the report keeps the
    # extractor error, the graph page the crawled URL paths feeding the tree.
    for html in (dashboard.render_html(data), dashboard.render_graph_html(data)):
        assert payload not in html, "the raw </script> payload reached the document"
        # Scraped text must contribute NO closing tag: every <script opens balances
        # a </script> close, none injected by the payload.
        assert html.count("<script") == html.count("</script>")
    # The escaped data still survives -- report keeps the extractor error verbatim.
    assert "onerror" in dashboard.render_html(data)


def test_top_external_targets_tally_counts_only_unfollowed_hosts(tmp_path):
    conn, db_file = _db(tmp_path)
    _doc(conn, "https://www.alpha.de/a")
    _doc(conn, "https://www.alpha.de/b")
    # internal + in-domain-subdomain edges (followed) plus two external targets.
    _link(conn, "https://www.alpha.de/a", "https://www.alpha.de/b", in_domain=True)
    _link(conn, "https://www.alpha.de/a", "https://moodle.alpha.de/x", in_domain=True)
    _link(conn, "https://www.alpha.de/a", "https://github.com/x", in_domain=False)
    _link(conn, "https://www.alpha.de/b", "https://github.com/y", in_domain=False)
    conn.commit()

    data = dashboard.collect_analysis(
        conn, sites=_sites(), min_words=50, db_path=db_file
    )
    ext = {x["host"]: x["n"] for x in data["links"]["top_external"]}
    # only in_domain=0 targets are tallied; followed (in-domain) edges are excluded.
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
    assert 'href="discovery.html"' in html  # link out to the full-screen tree
    assert 'class="disctree"' not in html  # tree is NOT inlined in the report
    assert "d3.hierarchy" not in html  # and neither is d3
    assert html.count("<script") == 1  # only the (inert) data island remains


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
    assert 'id="disc-site"' in graph  # campus selector
    assert 'class="disctree"' in graph  # tree mount
    assert 'id="report-data"' in graph  # its own JSON island
    assert "d3.hierarchy" in graph  # tree script wired up
    assert 'href="analysis.html"' in graph  # back-link to the report
    assert "Alpha" in graph  # site name in the selector
    # d3 was actually inlined (vendored file read + embedded), not just referenced.
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
    assert 'href="discovery.html"' in report  # report links to its sibling
    assert 'class="disctree"' not in report
    assert 'class="disctree"' in graph  # tree lives on the graph page
    assert 'href="analysis.html"' in graph  # which links back


def test_empty_queue_discovery_degrades_to_warnbox(tmp_path):
    conn, db_file = _db(tmp_path)
    _doc(conn, "https://www.alpha.de/a")  # a document, but nothing crawled in the queue
    conn.commit()

    data = dashboard.collect_analysis(
        conn, sites=_sites(), min_words=50, db_path=db_file
    )
    assert data["discovery"]["sites"] == []
    report = dashboard.render_html(data)
    graph = dashboard.render_graph_html(data)
    assert 'href="discovery.html"' not in report  # nothing to link to
    assert "No crawled URLs yet" in report  # report notes the absence
    assert 'class="disctree"' not in graph
    assert "No crawled URLs yet" in graph  # graph page degrades too


# --------------------------------------------------------------------------- #
# Crawl discovery tree (`_discovery_trees`, pure function over queue rows).    #
# rows: (url, site, discovered_from, depth, work_state). `site` is the stored  #
# allowed_domain; each site becomes one rooted tree.                           #
# --------------------------------------------------------------------------- #


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
    # synthetic root first, parent -1.
    assert nodes[0]["st"] == "root" and nodes[0]["p"] == -1
    by_u = _by_u(nodes)
    # the seed hangs off the synthetic root; deeper pages off their discover-parent.
    assert by_u["/"]["p"] == 0
    assert by_u["/studium"]["p"] == nodes.index(by_u["/"])
    assert by_u["/studium/bachelor"]["p"] == nodes.index(by_u["/studium"])
    # every parent index precedes its child (BFS order) -> stable to suffix-truncate.
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
    assert "/broken" not in us  # dead-end error leaf dropped


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
    assert site["total"] == 21  # root + 20 pages, before the cap
    for i, n in enumerate(site["nodes"]):  # referential integrity survives truncation
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
    # central portal is the default-selected tree.
    assert out["sites"][out["default"]]["name"] == "Portal"
