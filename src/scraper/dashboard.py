"""Static HTML analysis report for the scraped / extracted corpus.

:func:`collect_analysis` runs a set of **read-only** aggregate queries over the
SQLite DB and returns a plain, JSON-serializable dict. :func:`render_html` turns
that dict into a single self-contained HTML file -- inline CSS + a little JS, no
external assets -- so it opens straight from disk over ``file://``.

There is no server and no live refresh: to refresh the numbers, regenerate the
file (``dhbw-scraper report``) and reload the page. The report is fully
data-driven (site names, ``min_words`` and every finding come from the DB +
config), so re-running it after a crawl/extract reflects the new state.
"""

from __future__ import annotations

import html
import json
import math
import time
from collections import Counter, defaultdict, deque
from pathlib import Path
from urllib.parse import urlsplit

# Word-count histogram buckets (lo inclusive, hi exclusive; last is open-ended).
_WC_BUCKETS = [
    (0, 50),
    (50, 100),
    (100, 250),
    (250, 500),
    (500, 1000),
    (1000, 2500),
    (2500, 5000),
    (5000, None),
]


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _host(url: str) -> str:
    try:
        return urlsplit(url).netloc.lower()
    except ValueError:
        return "?"


def _percentiles(sorted_vals: list[int], ps: tuple[int, ...]) -> dict[str, int]:
    out: dict[str, int] = {}
    n = len(sorted_vals)
    for p in ps:
        if n == 0:
            out[str(p)] = 0
        else:
            out[str(p)] = sorted_vals[min(n - 1, int(p / 100 * n))]
    return out


# Crawl-discovery-tree caps. Rendering details kept in code (like _WC_BUCKETS and
# the old host-graph's max_nodes=24), not config.toml -- they bound the payload
# embedded in the static report, not the crawl.
_TREE_MAX_CHILDREN = 40  # fan-out per node before the tail folds into "+K more"
_TREE_MAX_NODES_PER_SITE = 3000  # per-site node budget (breadth-first)
_TREE_DEFAULT_DOMAIN = "www.dhbw.de"  # campus selected on first load


def _rel(url: str) -> tuple[str, str]:
    """``(path[?query], host)`` for a URL -- the compact per-node key. The host is
    kept separately so the client can rebuild the absolute URL for tooltips without
    repeating the scheme+host on every node."""
    sp = urlsplit(url)
    rel = sp.path or "/"
    if sp.query:
        rel += "?" + sp.query
    return rel, sp.netloc.lower()


def _build_tree(items, max_children: int, max_nodes: int):
    """Build one site's discovery tree from ``(url, discovered_from, state)`` rows.

    Returns ``(nodes, host, truncated, total)`` or ``None`` when nothing survives
    pruning. ``nodes[0]`` is a synthetic root; each real node is
    ``{"u": path[?query], "p": parent index, "st": state}`` (+ ``"h"`` host override
    when a node's host differs from the site host). Emitted breadth-first, so a
    node's parent always precedes it and a suffix cut keeps every index valid.
    """
    state_of = {}
    parent_of = {}
    for url, parent, state in items:
        state_of[url] = state
        parent_of[url] = parent
    if not state_of:
        return None

    ROOT = None  # sentinel parent for seeds / cross-site orphans
    children: dict = defaultdict(list)
    for url in state_of:
        p = parent_of[url]
        children[p if p in state_of else ROOT].append(url)

    site_host = Counter(_rel(u)[1] for u in state_of).most_common(1)[0][0]

    # Keep a node iff its subtree contains a `done` node -- drops error/pending
    # dead-end leaves (trap tails) but never severs the path to a real page.
    keep: set = set()
    size: dict = {}
    for seed in children[ROOT]:
        stack = [(seed, False)]
        while stack:
            url, done = stack.pop()
            if done:
                kids = [c for c in children.get(url, ()) if c in keep]
                if state_of.get(url) == "done" or kids:
                    keep.add(url)
                    size[url] = 1 + sum(size[c] for c in kids)
            else:
                stack.append((url, True))
                stack.extend((c, False) for c in children.get(url, ()))

    if not keep:
        return None

    def kept_children(url):
        cs = [c for c in children.get(url, ()) if c in keep]
        cs.sort(key=lambda c: (-size[c], c))  # largest subtrees first, then stable
        return cs

    nodes = [{"u": "", "p": -1, "st": "root"}]
    truncated = False
    dq = deque((c, 0) for c in kept_children(ROOT))
    while dq:
        if len(nodes) >= max_nodes:
            truncated = True
            break
        url, pidx = dq.popleft()
        rel, host = _rel(url)
        rec = {"u": rel, "p": pidx, "st": state_of.get(url, "done")}
        if host != site_host:
            rec["h"] = host
        idx = len(nodes)
        nodes.append(rec)

        kids = kept_children(url)
        for c in kids[:max_children]:
            dq.append((c, idx))
        hidden = len(kids) - max_children
        if hidden > 0:
            if len(nodes) < max_nodes:
                nodes.append({"u": f"+{hidden} more", "p": idx, "st": "more"})
            else:
                truncated = True

    return nodes, site_host, truncated, len(keep)


def _discovery_trees(
    rows, sites, *, max_children=_TREE_MAX_CHILDREN, max_nodes=_TREE_MAX_NODES_PER_SITE
) -> dict:
    """Assemble one collapsible crawl-discovery tree per site from ``queue`` rows.

    ``rows`` yields ``(url, site, discovered_from, depth, work_state)``; ``site`` is
    the stored ``allowed_domain``. Only crawled (in-domain) URLs live in ``queue``,
    so every tree is DHBW-only by construction. See :func:`_build_tree` for the
    per-site reduction (prune / child cap / node cap).

    Returns ``{"sites": [{name, host, nodes, truncated, total}], "default": idx}``
    ordered by config, with the central portal selected by default.
    """
    domain2name = {s.allowed_domain: s.name for s in sites}
    order = {s.allowed_domain: i for i, s in enumerate(sites)}

    by_site: dict = defaultdict(list)
    for url, site, parent, _depth, state in rows:
        by_site[site].append((url, parent, state))

    out = []
    for domain, items in by_site.items():
        built = _build_tree(items, max_children, max_nodes)
        if built is None:
            continue
        nodes, host, truncated, total = built
        out.append(
            {
                "domain": domain,
                "name": domain2name.get(domain, domain),
                "host": host,
                "nodes": nodes,
                "truncated": truncated,
                "total": total,
            }
        )

    out.sort(key=lambda s: (order.get(s["domain"], len(order)), s["name"]))
    default = next(
        (i for i, s in enumerate(out) if s["domain"] == _TREE_DEFAULT_DOMAIN), 0
    )
    for s in out:
        del s["domain"]  # internal ordering key only
    return {"sites": out, "default": default}


def collect_analysis(conn, *, sites, min_words: int, db_path: Path) -> dict:
    """Run every read-only query and assemble the report payload.

    ``sites`` is ``config.sites`` (used to map the stored ``allowed_domain`` to a
    friendly name and to order the per-site table); ``min_words`` is
    ``config.extract.min_words`` (the current quality-gate threshold, used to flag
    documents that predate it).
    """
    cur = conn.cursor()

    def q(sql, args=()):
        return cur.execute(sql, args).fetchall()

    def one(sql, args=()):
        row = cur.execute(sql, args).fetchone()
        return row[0] if row and row[0] is not None else 0

    domain2name = {s.allowed_domain: s.name for s in sites}
    site_order = [s.allowed_domain for s in sites]
    name_of = lambda d: domain2name.get(d, d)  # noqa: E731

    # ---- db meta -------------------------------------------------------------
    st = db_path.stat()
    meta = {
        "generated_at": _now(),
        "db_path": str(db_path),
        "db_size_bytes": st.st_size,
        "db_mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(st.st_mtime)),
        "min_words": min_words,
        "raw_dir_bytes": one("SELECT sum(bytes) FROM raw_docs"),
    }

    # ---- corpus totals -------------------------------------------------------
    docs_present = one("SELECT count(*) FROM documents WHERE present=1")
    words_total = one("SELECT sum(word_count) FROM documents WHERE present=1")
    by_type = {
        st_: {
            "docs": n,
            "words": w or 0,
            "avg": (w or 0) / n if n else 0,
            "max": mx or 0,
        }
        for st_, n, w, mx in q(
            "SELECT source_type,count(*),sum(word_count),max(word_count) "
            "FROM documents WHERE present=1 GROUP BY source_type"
        )
    }

    queue_total = one("SELECT count(*) FROM queue")
    queue_state = {
        k: n for k, n in q("SELECT work_state,count(*) FROM queue GROUP BY work_state")
    }
    raw_total = one("SELECT count(*) FROM raw_docs")
    raw_by_type = {
        k: {"n": n, "bytes": b or 0}
        for k, n, b in q(
            "SELECT source_type,count(*),sum(bytes) FROM raw_docs GROUP BY source_type"
        )
    }
    quality_ok = one("SELECT count(*) FROM raw_docs WHERE quality_ok=1")
    quality_bad = one("SELECT count(*) FROM raw_docs WHERE quality_ok=0")
    extract_errored = one(
        "SELECT count(*) FROM raw_docs WHERE extract_error IS NOT NULL"
    )
    extract_pending = one("SELECT count(*) FROM raw_docs WHERE extract_state='pending'")

    # ---- reconciliation (dedup by text) -------------------------------------
    candidates = one(
        "SELECT count(*) FROM queue q JOIN raw_docs r ON r.content_sha256=q.content_sha256 "
        "WHERE q.present=1 AND r.quality_ok=1"
    )
    distinct_text = one(
        "SELECT count(DISTINCT text_sha256) FROM documents WHERE present=1"
    )

    # ---- per-site ------------------------------------------------------------
    dsite = {
        d: {"docs": n, "words": w or 0, "avg": (w or 0) / n if n else 0}
        for d, n, w in q(
            "SELECT site,count(*),sum(word_count) FROM documents WHERE present=1 GROUP BY site"
        )
    }
    qsite = defaultdict(Counter)
    for d, ws, n in q(
        "SELECT site,work_state,count(*) FROM queue GROUP BY site,work_state"
    ):
        qsite[d][ws] += n
    seen = set(dsite) | set(qsite)
    ordered = site_order + [d for d in seen if d not in site_order]
    per_site = []
    for d in ordered:
        if d not in seen:
            continue
        dd = dsite.get(d, {"docs": 0, "words": 0, "avg": 0})
        qq = qsite.get(d, Counter())
        done, err = qq.get("done", 0), qq.get("error", 0)
        per_site.append(
            {
                "name": name_of(d),
                "domain": d,
                "docs": dd["docs"],
                "words": dd["words"],
                "avg": round(dd["avg"]),
                "done": done,
                "error": err,
                "pending": qq.get("pending", 0),
                "error_rate": err / (done + err) if (done + err) else 0.0,
            }
        )

    # ---- extraction rejects / errors ----------------------------------------
    reject_reasons = [
        {"reason": r or "(none)", "n": n}
        for r, n in q(
            "SELECT reject_reason,count(*) FROM raw_docs WHERE reject_reason IS NOT NULL "
            "GROUP BY reject_reason ORDER BY 2 DESC LIMIT 12"
        )
    ]
    extract_errors = [
        {"error": (e or "")[:140], "n": n}
        for e, n in q(
            "SELECT extract_error,count(*) FROM raw_docs WHERE extract_error IS NOT NULL "
            "GROUP BY extract_error ORDER BY 2 DESC LIMIT 12"
        )
    ]

    # ---- word-count distribution --------------------------------------------
    wc = [
        r[0]
        for r in q(
            "SELECT word_count FROM documents WHERE present=1 ORDER BY word_count"
        )
    ]
    hist = []
    for lo, hi in _WC_BUCKETS:
        if hi is None:
            n = sum(1 for w in wc if w >= lo)
            label = f"{lo:,}+"
        else:
            n = sum(1 for w in wc if lo <= w < hi)
            label = f"{lo:,}-{hi:,}"
        hist.append({"label": label, "n": n})
    wordcount = {
        "n": len(wc),
        "min": wc[0] if wc else 0,
        "max": wc[-1] if wc else 0,
        "pct": _percentiles(wc, (10, 25, 50, 75, 90, 99)),
        "hist": hist,
        "below_min": sum(1 for w in wc if w < min_words),
    }

    # ---- crawl_log -----------------------------------------------------------
    crawl_log = {
        "total": one("SELECT count(*) FROM crawl_log"),
        "outcomes": [
            {"k": k or "(none)", "n": n}
            for k, n in q(
                "SELECT outcome,count(*) FROM crawl_log GROUP BY outcome ORDER BY 2 DESC"
            )
        ],
        "status": [
            {"k": k if k is not None else 0, "n": n}
            for k, n in q(
                "SELECT status,count(*) FROM crawl_log GROUP BY status ORDER BY 2 DESC LIMIT 12"
            )
        ],
        "errors": [
            {"k": (k or "")[:100], "n": n}
            for k, n in q(
                "SELECT error,count(*) FROM crawl_log WHERE error IS NOT NULL AND error<>'' "
                "GROUP BY error ORDER BY 2 DESC LIMIT 10"
            )
        ],
    }
    err_by_site = defaultdict(Counter)
    for d, s_, n in q(
        "SELECT site,http_status,count(*) FROM queue WHERE work_state='error' "
        "GROUP BY site,http_status"
    ):
        err_by_site[d][s_ if s_ is not None else 0] += n
    errors_by_site = [
        {
            "name": name_of(d),
            "total": sum(c.values()),
            "breakdown": ", ".join(f"{k}:{v}" for k, v in c.most_common(6)),
        }
        for d, c in sorted(err_by_site.items(), key=lambda kv: -sum(kv[1].values()))
    ]

    # ---- link graph ----------------------------------------------------------
    links_total = one("SELECT count(*) FROM links")
    links = {
        "total": links_total,
        "in_domain": one("SELECT count(*) FROM links WHERE in_domain=1"),
        "external": one("SELECT count(*) FROM links WHERE in_domain=0"),
        "distinct_src": one("SELECT count(DISTINCT src_id) FROM links"),
        "distinct_dst": one("SELECT count(DISTINCT dst_id) FROM links"),
    }
    links["docs_no_out"] = (
        one(
            "SELECT count(*) FROM documents d WHERE present=1 "
            "AND NOT EXISTS (SELECT 1 FROM urls u JOIN links l ON l.src_id=u.id "
            "WHERE u.url=d.url)"
        )
        if links_total
        else docs_present
    )

    # Top external link targets: hosts we linked to but did not follow
    # (in_domain=0). Streams only the ~1.4M external edges, not the full 15M.
    ext_hosts = Counter(
        _host(u)
        for (u,) in q(
            "SELECT d.url FROM links l JOIN urls d ON d.id = l.dst_id "
            "WHERE l.in_domain = 0"
        )
    )
    ext_hosts.pop("", None)
    links["top_external"] = [{"host": h, "n": n} for h, n in ext_hosts.most_common(20)]

    # ---- per-host (spider-trap view) ----------------------------------------
    hostc = Counter()
    hoststate = defaultdict(Counter)
    for url, ws in q("SELECT url,work_state FROM queue"):
        h = _host(url)
        hostc[h] += 1
        hoststate[h][ws] += 1
    hosts = [
        {
            "host": h,
            "urls": n,
            "done": hoststate[h].get("done", 0),
            "error": hoststate[h].get("error", 0),
            "pending": hoststate[h].get("pending", 0),
        }
        for h, n in hostc.most_common(25)
    ]

    # ---- crawl discovery tree (per-site, DHBW-only by construction) ----------
    discovery = _discovery_trees(
        q("SELECT url, site, discovered_from, depth, work_state FROM queue"), sites
    )

    # ---- freshness -----------------------------------------------------------
    def rng(col, table, where=""):
        w = f" WHERE {where}" if where else ""
        return {
            "min": one(f"SELECT min({col}) FROM {table}{w}") or "",
            "max": one(f"SELECT max({col}) FROM {table}{w}") or "",
        }

    freshness = {
        "queue_first_seen": rng("first_seen_at", "queue"),
        "documents_updated": rng("updated_at", "documents"),
        "raw_first_seen": rng("first_seen_at", "raw_docs"),
    }

    data = {
        "meta": meta,
        "totals": {
            "documents": docs_present,
            "words": words_total,
            "by_type": by_type,
            "queue_total": queue_total,
            "queue_state": queue_state,
            "raw_total": raw_total,
            "raw_by_type": raw_by_type,
            "quality_ok": quality_ok,
            "quality_bad": quality_bad,
            "extract_errored": extract_errored,
            "extract_pending": extract_pending,
            "candidates": candidates,
            "distinct_text": distinct_text,
        },
        "per_site": per_site,
        "reject_reasons": reject_reasons,
        "extract_errors": extract_errors,
        "wordcount": wordcount,
        "crawl_log": crawl_log,
        "errors_by_site": errors_by_site,
        "links": links,
        "hosts": hosts,
        "discovery": discovery,
        "freshness": freshness,
    }
    data["findings"] = _findings(data)
    return data


def _findings(d: dict) -> list[dict]:
    """Derive ranked findings from the payload so the report stays accurate on
    every refresh (a re-crawl that fixes a site makes its finding disappear)."""
    out: list[dict] = []
    sites = d["per_site"]
    docs = [s["docs"] for s in sites if s["docs"] > 0]
    median = sorted(docs)[len(docs) // 2] if docs else 0

    # Under-covered sites (tiny corpus relative to peers).
    for s in sites:
        if median and s["docs"] < max(200, 0.15 * median):
            sev = "crit" if s["docs"] < 0.05 * median else "warn"
            out.append(
                {
                    "sev": sev,
                    "title": f"{s['name']} is under-covered",
                    "detail": f"{s['docs']:,} documents vs a per-site median of {median:,}; "
                    f"queue shows {s['error']:,} errors "
                    f"({s['error_rate']*100:.0f}% of fetch attempts).",
                }
            )

    # Documents below the current min_words gate.
    bm = d["wordcount"]["below_min"]
    if bm:
        pct = bm / max(1, d["totals"]["documents"]) * 100
        out.append(
            {
                "sev": "warn",
                "title": f"{bm:,} documents below min_words={d['meta']['min_words']}",
                "detail": f"{pct:.0f}% of the corpus is thinner than the current quality gate "
                f"(min {d['wordcount']['min']} words) -- legacy rows from an extract "
                f"pass with a looser threshold. A full re-extract would normalize them.",
            }
        )

    # Link graph underpopulated.
    lk = d["links"]
    if d["totals"]["documents"] and lk["distinct_src"] < 0.5 * d["totals"]["documents"]:
        out.append(
            {
                "sev": "warn",
                "title": "Link graph is largely unpopulated",
                "detail": f"{lk['total']:,} edges from only {lk['distinct_src']:,} source pages; "
                f"{lk['docs_no_out']:,} of {d['totals']['documents']:,} documents have "
                f"zero outbound edges. Investigate the links write path before relying on it.",
            }
        )

    # Error hotspots not already flagged as under-covered.
    flagged = {
        f["title"].split(" is under-covered")[0]
        for f in out
        if "under-covered" in f["title"]
    }
    for s in sites:
        if s["name"] not in flagged and s["error"] >= 100 and s["error_rate"] > 0.10:
            out.append(
                {
                    "sev": "info",
                    "title": f"{s['name']} has elevated fetch errors",
                    "detail": f"{s['error']:,} errored URLs ({s['error_rate']*100:.0f}% of attempts) "
                    f"-- mostly connection refused/timeout, worth one gentle retry.",
                }
            )

    sev_order = {"crit": 0, "warn": 1, "info": 2, "ok": 3}
    out.sort(key=lambda f: sev_order.get(f["sev"], 9))
    if not out:
        out.append(
            {
                "sev": "ok",
                "title": "No issues detected",
                "detail": "Coverage, extraction, and the link graph all look healthy.",
            }
        )
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"


def _esc(x) -> str:
    return html.escape(str(x))


def _link_graph_svg(g: dict) -> str:
    """Render the host-level link graph as a self-contained inline SVG.

    Deterministic circular layout (no randomness, no external libs): ``site`` hosts
    are clustered before ``external`` ones and placed evenly on a ring; cross-host
    edges are drawn as centre-bundled Bezier curves whose width tracks their weight.
    Colours are CSS custom properties, so the graph re-themes with the page toggle.
    Falls back to a warning box when there is nothing (cross-host) to draw.
    """
    nodes, edges = g["nodes"], g["edges"]
    if not nodes or not edges:
        return (
            '<p class="warnbox">No cross-host links to graph yet — re-run the '
            "crawl (<code>fetch</code>) to rebuild the edge list from stored "
            "HTML, then regenerate this report.</p>"
        )

    w, h = 820, 560
    cx, cy, r_ring = w / 2, h / 2, 195
    n = len(nodes)
    # Cluster site hosts before external, each group by descending degree.
    order = sorted(
        range(n), key=lambda i: (nodes[i]["kind"] != "site", -nodes[i]["deg"])
    )
    pos: dict[int, tuple[float, float, float]] = {}
    for slot, i in enumerate(order):
        ang = 2 * math.pi * slot / n - math.pi / 2
        pos[i] = (cx + r_ring * math.cos(ang), cy + r_ring * math.sin(ang), ang)

    max_w = max(e["w"] for e in edges)
    max_deg = max(nd["deg"] for nd in nodes) or 1
    ln_max_w = math.log1p(max_w) or 1.0

    edge_svg = []
    for e in edges:
        x1, y1, _ = pos[e["s"]]
        x2, y2, _ = pos[e["t"]]
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        qx, qy = mx + (cx - mx) * 0.35, my + (cy - my) * 0.35  # bundle toward centre
        sw = 0.6 + 3.4 * math.log1p(e["w"]) / ln_max_w
        edge_svg.append(
            f'<path d="M{x1:.1f},{y1:.1f} Q{qx:.1f},{qy:.1f} {x2:.1f},{y2:.1f}" '
            f'fill="none" stroke="var(--bar)" stroke-width="{sw:.2f}" '
            f'stroke-opacity="0.3" marker-end="url(#lg-arrow)"/>'
        )

    node_svg, label_svg = [], []
    for i, nd in enumerate(nodes):
        x, y, ang = pos[i]
        rad = 4 + 11 * math.sqrt(nd["deg"] / max_deg)
        fill = "var(--accent)" if nd["kind"] == "site" else "var(--muted)"
        tip = (
            f'{nd["host"]} — {nd["out"]:,} out · {nd["in"]:,} in · '
            f'{nd["self"]:,} internal'
        )
        node_svg.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{rad:.1f}" fill="{fill}" '
            f'fill-opacity="0.9" stroke="var(--card)" stroke-width="1.5">'
            f"<title>{_esc(tip)}</title></circle>"
        )
        lx = cx + (r_ring + 14) * math.cos(ang)
        ly = cy + (r_ring + 14) * math.sin(ang)
        anchor = "start" if math.cos(ang) >= 0 else "end"
        label = nd["host"] if len(nd["host"]) <= 22 else nd["host"][:21] + "…"
        label_svg.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" '
            f'dominant-baseline="middle" font-size="10" fill="var(--muted)">'
            f"{_esc(label)}</text>"
        )

    defs = (
        '<defs><marker id="lg-arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" fill="var(--bar)" fill-opacity="0.55"/>'
        "</marker></defs>"
    )
    return (
        f'<svg class="linkgraph" viewBox="0 0 {w} {h}" width="100%" '
        f'role="img" aria-label="Host-level link graph">{defs}'
        f'{"".join(edge_svg)}{"".join(node_svg)}{"".join(label_svg)}</svg>'
    )


def render_html(d: dict) -> str:
    m = d["meta"]
    t = d["totals"]

    def kpi(label, value, sub=""):
        sub_html = f'<div class="kpi-sub">{_esc(sub)}</div>' if sub else ""
        return (
            f'<div class="kpi"><div class="kpi-val">{_esc(value)}</div>'
            f'<div class="kpi-label">{_esc(label)}</div>{sub_html}</div>'
        )

    # KPI row
    html_t = t["by_type"].get("html", {})
    pdf_t = t["by_type"].get("pdf", {})
    kpis = "".join(
        [
            kpi("Documents", f"{t['documents']:,}", "present, deduplicated"),
            kpi(
                "Total words",
                f"{t['words']/1e6:.1f} M",
                f"HTML {html_t.get('words',0)/1e6:.1f}M · PDF {pdf_t.get('words',0)/1e6:.1f}M",
            ),
            kpi(
                "Raw blobs",
                f"{t['raw_total']:,}",
                _fmt_bytes(m["raw_dir_bytes"]) + " cached",
            ),
            kpi(
                "Extraction pass",
                f"{t['quality_ok']/max(1,t['raw_total'])*100:.1f}%",
                f"{t['quality_bad']:,} rejected · {t['extract_errored']:,} errored",
            ),
            kpi(
                "URLs crawled",
                f"{t['queue_total']:,}",
                f"{t['queue_state'].get('done',0):,} done · {t['queue_state'].get('error',0):,} error",
            ),
            kpi(
                "Text-dedup",
                f"{(t['candidates']-t['documents'])/max(1,t['candidates'])*100:.0f}%",
                f"{t['candidates']:,} candidates → {t['documents']:,} unique",
            ),
        ]
    )

    # Findings
    sev_label = {"crit": "CRITICAL", "warn": "WARNING", "info": "INFO", "ok": "OK"}
    findings = "".join(
        f'<div class="finding {f["sev"]}"><span class="badge {f["sev"]}">{sev_label[f["sev"]]}</span>'
        f'<div><div class="f-title">{_esc(f["title"])}</div>'
        f'<div class="f-detail">{_esc(f["detail"])}</div></div></div>'
        for f in d["findings"]
    )

    # Per-site table
    max_docs = max((s["docs"] for s in d["per_site"]), default=1) or 1
    site_rows = ""
    for s in d["per_site"]:
        bar = s["docs"] / max_docs * 100
        er = s["error_rate"] * 100
        er_cls = "hot" if er >= 10 else ("warm" if er >= 2 else "")
        site_rows += (
            f'<tr><td class="mono">{_esc(s["name"])}</td>'
            f'<td class="num">{s["docs"]:,}<div class="minibar"><span style="width:{bar:.1f}%"></span></div></td>'
            f'<td class="num">{s["avg"]:,}</td>'
            f'<td class="num">{s["done"]:,}</td>'
            f'<td class="num {er_cls}">{s["error"]:,}</td>'
            f'<td class="num {er_cls}">{er:.1f}%</td></tr>'
        )

    # Word-count histogram
    max_h = max((b["n"] for b in d["wordcount"]["hist"]), default=1) or 1
    hist_rows = "".join(
        f'<tr><td class="mono">{_esc(b["label"])}</td>'
        f'<td class="num">{b["n"]:,}</td>'
        f'<td class="barcell"><div class="minibar wide"><span style="width:{b["n"]/max_h*100:.1f}%"></span></div></td></tr>'
        for b in d["wordcount"]["hist"]
    )
    p = d["wordcount"]["pct"]
    pct_line = (
        f'min {d["wordcount"]["min"]} · p10 {p["10"]} · p25 {p["25"]} · '
        f'median {p["50"]} · p75 {p["75"]} · p90 {p["90"]} · p99 {p["99"]:,} · '
        f'max {d["wordcount"]["max"]:,}'
    )

    def simple_table(title, rows_html, headers):
        head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
        return (
            f"<section><h2>{_esc(title)}</h2><table><thead><tr>{head}</tr></thead>"
            f"<tbody>{rows_html}</tbody></table></section>"
        )

    reject_rows = (
        "".join(
            f'<tr><td class="mono">{_esc(r["reason"])}</td><td class="num">{r["n"]:,}</td></tr>'
            for r in d["reject_reasons"]
        )
        or '<tr><td colspan="2">none</td></tr>'
    )
    exerr_rows = (
        "".join(
            f'<tr><td class="mono">{_esc(e["error"])}</td><td class="num">{e["n"]:,}</td></tr>'
            for e in d["extract_errors"]
        )
        or '<tr><td colspan="2">none</td></tr>'
    )
    outcome_rows = "".join(
        f'<tr><td class="mono">{_esc(o["k"])}</td><td class="num">{o["n"]:,}</td></tr>'
        for o in d["crawl_log"]["outcomes"]
    )
    status_rows = "".join(
        f'<tr><td class="mono">{_esc(s["k"])}</td><td class="num">{s["n"]:,}</td></tr>'
        for s in d["crawl_log"]["status"]
    )
    clerr_rows = (
        "".join(
            f'<tr><td class="mono">{_esc(e["k"])}</td><td class="num">{e["n"]:,}</td></tr>'
            for e in d["crawl_log"]["errors"]
        )
        or '<tr><td colspan="2">none</td></tr>'
    )
    errsite_rows = (
        "".join(
            f'<tr><td class="mono">{_esc(e["name"])}</td><td class="num">{e["total"]:,}</td>'
            f'<td class="mono small">{_esc(e["breakdown"])}</td></tr>'
            for e in d["errors_by_site"]
        )
        or '<tr><td colspan="3">none</td></tr>'
    )
    ext_rows = (
        "".join(
            f'<tr><td class="mono">{_esc(x["host"])}</td><td class="num">{x["n"]:,}</td></tr>'
            for x in d["links"]["top_external"]
        )
        or '<tr><td colspan="2">none</td></tr>'
    )
    host_rows = ""
    for h in d["hosts"]:
        er_cls = "hot" if h["urls"] and h["error"] / h["urls"] >= 0.3 else ""
        host_rows += (
            f'<tr><td class="mono">{_esc(h["host"])}</td>'
            f'<td class="num">{h["urls"]:,}</td><td class="num">{h["done"]:,}</td>'
            f'<td class="num {er_cls}">{h["error"]:,}</td>'
            f'<td class="num">{h["pending"]:,}</td></tr>'
        )

    lk = d["links"]
    links_warn = ""
    if t["documents"] and lk["distinct_src"] < 0.5 * t["documents"]:
        links_warn = (
            '<p class="warnbox">⚠ Only '
            f'{lk["distinct_src"]:,} source pages recorded any links '
            f'({lk["docs_no_out"]:,} of {t["documents"]:,} documents have none) — '
            'the link graph is effectively unpopulated and should not be trusted yet.</p>'
        )

    # Host-level link graph (static SVG) + its caption/legend.
    g = lk["graph"]
    graph_svg = _link_graph_svg(g)
    if g["nodes"] and g["edges"]:
        graph_caption = (
            f'<div class="sub" style="margin:2px 0 10px">Host-level view · '
            f'{g["n_hosts"]:,} hosts total, showing the {len(g["nodes"])} most-connected · '
            f'{g["n_edges_shown"]:,} of {g["n_edges_total"]:,} cross-host links drawn.</div>'
        )
        graph_legend = (
            '<div class="graph-legend">'
            '<span><i class="sw site"></i>crawled host</span>'
            '<span><i class="sw ext"></i>external host</span>'
            "<span>node size = degree</span>"
            "<span>edge width = link count</span></div>"
        )
    else:
        graph_caption = graph_legend = ""

    fr = d["freshness"]
    # `</` is neutralised before this lands inside <script>...</script>. json.dumps
    # escapes quotes and control characters but not a closing tag, and `d` carries
    # free text straight from the DB -- extractor and crawl_log errors that quote
    # whatever a scraped page contained. A single `</script>` substring would close
    # the element early and turn the rest of the payload into live DOM in the
    # operator's browser. `<\/` is the same string to a JSON parser, so the data
    # survives intact; every visible sink already goes through _esc().
    payload = json.dumps(d, ensure_ascii=False, indent=0).replace("</", "<\\/")

    return f"""<title>DHBW corpus report</title>
<style>
:root {{
  --bg:#f6f7f9; --card:#fff; --ink:#1a1d21; --muted:#6b7280; --line:#e5e7eb;
  --accent:#2563eb; --crit:#dc2626; --warn:#d97706; --info:#2563eb; --ok:#059669;
  --bar:#3b82f6; --hot:#dc2626; --warm:#d97706;
}}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#0d1117; --card:#161b22; --ink:#e6edf3; --muted:#8b949e; --line:#30363d;
          --accent:#58a6ff; --bar:#388bfd; }}
}}
:root[data-theme="dark"] {{ --bg:#0d1117; --card:#161b22; --ink:#e6edf3; --muted:#8b949e;
  --line:#30363d; --accent:#58a6ff; --bar:#388bfd; }}
:root[data-theme="light"] {{ --bg:#f6f7f9; --card:#fff; --ink:#1a1d21; --muted:#6b7280;
  --line:#e5e7eb; --accent:#2563eb; --bar:#3b82f6; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }}
.wrap {{ max-width:1080px; margin:0 auto; padding:28px 20px 80px; }}
header {{ display:flex; flex-wrap:wrap; align-items:baseline; gap:12px; justify-content:space-between; }}
h1 {{ font-size:22px; margin:0; }}
h2 {{ font-size:15px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted);
  margin:34px 0 10px; border-bottom:1px solid var(--line); padding-bottom:6px; }}
.sub {{ color:var(--muted); font-size:12.5px; }}
.sub code {{ background:var(--card); border:1px solid var(--line); border-radius:5px; padding:1px 6px; }}
.kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-top:18px; }}
.kpi {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }}
.kpi-val {{ font-size:24px; font-weight:650; letter-spacing:-.02em; }}
.kpi-label {{ color:var(--muted); font-size:12px; margin-top:2px; }}
.kpi-sub {{ color:var(--muted); font-size:11px; margin-top:5px; }}
.finding {{ display:flex; gap:12px; background:var(--card); border:1px solid var(--line);
  border-left-width:4px; border-radius:8px; padding:12px 14px; margin-bottom:8px; }}
.finding.crit {{ border-left-color:var(--crit); }}
.finding.warn {{ border-left-color:var(--warn); }}
.finding.info {{ border-left-color:var(--info); }}
.finding.ok {{ border-left-color:var(--ok); }}
.f-title {{ font-weight:600; }}
.f-detail {{ color:var(--muted); font-size:13px; margin-top:2px; }}
.badge {{ font-size:10px; font-weight:700; padding:2px 7px; border-radius:20px; height:fit-content;
  color:#fff; white-space:nowrap; }}
.badge.crit {{ background:var(--crit); }} .badge.warn {{ background:var(--warn); }}
.badge.info {{ background:var(--info); }} .badge.ok {{ background:var(--ok); }}
.grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:22px; }}
@media (max-width:720px) {{ .grid2 {{ grid-template-columns:1fr; }} }}
table {{ width:100%; border-collapse:collapse; background:var(--card);
  border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
th,td {{ text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); }}
th {{ font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); font-weight:600; }}
tr:last-child td {{ border-bottom:none; }}
td.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
td.mono, .mono {{ font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:12.5px; }}
.small {{ font-size:11px; color:var(--muted); }}
.hot {{ color:var(--hot); font-weight:600; }} .warm {{ color:var(--warm); }}
.minibar {{ height:4px; background:var(--line); border-radius:3px; margin-top:4px; }}
.minibar.wide {{ height:9px; margin:0; }}
.minibar > span {{ display:block; height:100%; background:var(--bar); border-radius:3px; }}
.barcell {{ width:45%; }}
.warnbox {{ background:color-mix(in srgb,var(--warn) 12%,transparent); border:1px solid var(--warn);
  border-radius:8px; padding:10px 12px; font-size:13px; }}
.graph-legend {{ display:flex; flex-wrap:wrap; gap:14px; margin:0 0 10px; color:var(--muted);
  font-size:11.5px; align-items:center; }}
.graph-legend .sw {{ display:inline-block; width:10px; height:10px; border-radius:50%;
  margin-right:5px; vertical-align:-1px; }}
.graph-legend .sw.site {{ background:var(--accent); }}
.graph-legend .sw.ext {{ background:var(--muted); }}
svg.linkgraph {{ display:block; width:100%; max-width:820px; height:auto; margin:0 auto;
  background:var(--card); border:1px solid var(--line); border-radius:10px; }}
.toggle {{ cursor:pointer; background:var(--card); border:1px solid var(--line); color:var(--ink);
  border-radius:6px; padding:5px 10px; font-size:12px; }}
footer {{ margin-top:40px; color:var(--muted); font-size:12px; border-top:1px solid var(--line); padding-top:14px; }}
</style>

<div class="wrap">
  <header>
    <div>
      <h1>DHBW corpus — scrape &amp; extract report</h1>
      <div class="sub">Generated {_esc(m['generated_at'])} UTC · DB {_fmt_bytes(m['db_size_bytes'])}
        (modified {_esc(m['db_mtime'])}) · gate <code>min_words={m['min_words']}</code></div>
    </div>
    <button class="toggle" onclick="var r=document.documentElement;r.dataset.theme=r.dataset.theme==='dark'?'light':'dark'">◐ theme</button>
  </header>

  <div class="sub" style="margin-top:8px">To refresh: re-run <code>uv run dhbw-scraper report</code> and reload this page.</div>

  <div class="kpis">{kpis}</div>

  <section><h2>Findings</h2>{findings}</section>

  <section><h2>Per-site coverage</h2>
    <table><thead><tr><th>Site</th><th class="num">Documents</th><th class="num">Avg words</th>
      <th class="num">Fetched</th><th class="num">Errors</th><th class="num">Error rate</th></tr></thead>
      <tbody>{site_rows}</tbody></table></section>

  <section><h2>Word-count distribution</h2>
    <div class="sub" style="margin-bottom:8px">{_esc(pct_line)}</div>
    <table><thead><tr><th>Words</th><th class="num">Docs</th><th></th></tr></thead>
      <tbody>{hist_rows}</tbody></table></section>

  <div class="grid2">
    {simple_table("Quality-gate rejects", reject_rows, ["Reason", "Count"])}
    {simple_table("Extraction errors", exerr_rows, ["Error", "Count"])}
    {simple_table("Crawl outcomes", outcome_rows, ["Outcome", "Count"])}
    {simple_table("HTTP status", status_rows, ["Status", "Count"])}
  </div>

  {simple_table("Top crawl errors", clerr_rows, ["Error", "Count"])}
  {simple_table("Errors by site (work_state=error)", errsite_rows, ["Site", "Errors", "By HTTP status"])}

  <section><h2>Link graph</h2>
    {links_warn}
    <div class="sub">{lk['total']:,} edges · {lk['in_domain']:,} in-domain · {lk['external']:,} external ·
      {lk['distinct_src']:,} distinct sources · {lk['distinct_dst']:,} distinct targets</div>
    {graph_caption}
    {graph_legend}
    {graph_svg}
  </section>
  {simple_table("Top external link targets", ext_rows, ["Host", "Links"])}

  {simple_table("Per-host URL distribution (spider-trap view)", host_rows, ["Host", "URLs", "Done", "Error", "Pending"])}

  <footer>
    Crawl window {_esc(fr['queue_first_seen']['min'])} → {_esc(fr['raw_first_seen']['max'])} UTC ·
    documents updated {_esc(fr['documents_updated']['min'])} → {_esc(fr['documents_updated']['max'])} UTC.<br>
    Read-only snapshot of <span class="mono">{_esc(m['db_path'])}</span>. Static file — regenerate to refresh.
  </footer>
</div>
<script id="report-data" type="application/json">{payload}</script>
"""


def write_report(conn, *, sites, min_words: int, db_path: Path, out_path: Path) -> Path:
    data = collect_analysis(conn, sites=sites, min_words=min_words, db_path=db_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("<!doctype html>\n" + render_html(data), encoding="utf-8")
    return out_path
