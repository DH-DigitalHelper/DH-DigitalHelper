import types

from dhbw_scraper import dashboard
from dhbw_scraper import storage as st

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


def _link(conn, src, dst, *, in_domain, site="alpha.de", depth=1):
    conn.execute(
        "INSERT OR IGNORE INTO links (src_url, dst_url, site, in_domain, depth, first_seen_at) "
        "VALUES (?,?,?,?,?,?)",
        (src, dst, site, 1 if in_domain else 0, depth, NOW),
    )


def test_graph_payload_has_nodes_edges_and_kinds(tmp_path):
    conn, db_file = _db(tmp_path)
    _doc(conn, "https://www.alpha.de/a")
    _doc(conn, "https://www.alpha.de/b")
    # self-loop (same host), a cross-subdomain in-domain edge, and two externals.
    _link(conn, "https://www.alpha.de/a", "https://www.alpha.de/b", in_domain=True)
    _link(conn, "https://www.alpha.de/a", "https://moodle.alpha.de/x", in_domain=True)
    _link(conn, "https://www.alpha.de/a", "https://github.com/x", in_domain=False)
    _link(conn, "https://www.alpha.de/b", "https://github.com/y", in_domain=False)
    conn.commit()

    data = dashboard.collect_analysis(
        conn, sites=_sites(), min_words=50, db_path=db_file
    )
    g = data["links"]["graph"]
    kinds = {n["host"]: n["kind"] for n in g["nodes"]}

    assert kinds["www.alpha.de"] == "site"          # appeared as a source
    assert kinds["moodle.alpha.de"] == "external"   # dst-only subdomain
    assert kinds["github.com"] == "external"
    # cross-host edges only (the self-loop is not an edge); github weight == 2.
    weights = {
        (g["nodes"][e["s"]]["host"], g["nodes"][e["t"]]["host"]): e["w"]
        for e in g["edges"]
    }
    assert weights[("www.alpha.de", "github.com")] == 2
    assert weights[("www.alpha.de", "moodle.alpha.de")] == 1
    assert g["n_edges_total"] == 3 and g["n_edges_shown"] == 3
    # the self-loop is tracked on the node, not as an edge.
    src_node = next(n for n in g["nodes"] if n["host"] == "www.alpha.de")
    assert src_node["self"] == 1


def test_render_html_includes_graph_svg(tmp_path):
    conn, db_file = _db(tmp_path)
    _doc(conn, "https://www.alpha.de/a")
    _link(conn, "https://www.alpha.de/a", "https://github.com/x", in_domain=False)
    conn.commit()

    data = dashboard.collect_analysis(
        conn, sites=_sites(), min_words=50, db_path=db_file
    )
    html = dashboard.render_html(data)
    assert 'class="linkgraph"' in html
    assert "github.com" in html
    assert "cross-host links drawn" in html


def test_empty_link_graph_degrades_to_warnbox(tmp_path):
    conn, db_file = _db(tmp_path)
    _doc(conn, "https://www.alpha.de/a")  # documents but no links
    conn.commit()

    data = dashboard.collect_analysis(
        conn, sites=_sites(), min_words=50, db_path=db_file
    )
    assert data["links"]["graph"]["nodes"] == []
    html = dashboard.render_html(data)
    assert 'class="linkgraph"' not in html   # no SVG rendered
    assert "backfill-links" in html          # the graceful fallback message
