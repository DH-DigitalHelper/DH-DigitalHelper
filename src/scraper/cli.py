"""Command-line entrypoint: fetch / extract / run / stats / dedup / delta /
backfill-links."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from . import crawl, extract, storage
from .config import load_config


def _run_id() -> str:
    return time.strftime("run-%Y%m%dT%H%M%S", time.gmtime())


def _load(args):
    return load_config(Path(args.config) if args.config else None)


def _resolve_sites(config, names):
    """Filter ``config.sites`` to those whose ``name`` or ``allowed_domain`` is in
    ``names``. Exits with a clear message if any requested name matches nothing, so
    a typo fails loudly instead of silently crawling the wrong (or every) site."""
    unmatched = [
        n
        for n in names
        if not any(n in (s.name, s.allowed_domain) for s in config.sites)
    ]
    if unmatched:
        available = ", ".join(f"{s.name} ({s.allowed_domain})" for s in config.sites)
        raise SystemExit(
            f"--site: no configured site matches {unmatched}. Available: {available}"
        )
    return [s for s in config.sites if s.name in names or s.allowed_domain in names]


def _cmd_fetch(args) -> int:
    config = _load(args)
    if args.site:
        object.__setattr__(config, "sites", _resolve_sites(config, args.site))
    if args.max_pages is not None:
        object.__setattr__(config.crawl, "max_pages", args.max_pages)
    if args.max_pages_per_host is not None:
        object.__setattr__(config.crawl, "max_pages_per_host", args.max_pages_per_host)
    if args.workers_per_host is not None:
        object.__setattr__(config.crawl, "workers_per_host", args.workers_per_host)
    if args.request_delay is not None:
        object.__setattr__(config.crawl, "request_delay_seconds", args.request_delay)
    if args.changed_only:
        object.__setattr__(config.crawl, "recheck", "changed-only")
    elif args.new_only:
        object.__setattr__(config.crawl, "recheck", "new-only")
    elif args.full:
        object.__setattr__(config.crawl, "recheck", "force-full")
    results = crawl.run_fetch(config, _run_id())
    for site, counts in results.items():
        print(f"[{site}] " + " ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


def _run_extract(args, source_type) -> int:
    config = _load(args)
    if args.workers is not None:
        object.__setattr__(config.extract, "workers", args.workers)
    counts = extract.run_extract(config, source_type=source_type)
    print(" ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


def _cmd_extract(args) -> int:
    return _run_extract(args, None)


def _cmd_extract_html(args) -> int:
    return _run_extract(args, "html")


def _cmd_extract_pdf(args) -> int:
    return _run_extract(args, "pdf")


def _cmd_run(args) -> int:
    rc = _cmd_fetch(args)
    if rc:
        return rc
    rc = _cmd_extract(args)
    return rc or _cmd_dedup(args)


def _cmd_stats(args) -> int:
    config = _load(args)
    conn = storage.connect(config.storage.db_file)
    storage.init_db(conn)
    print(json.dumps(storage.stats(conn), indent=2))
    conn.close()
    return 0


def _cmd_delta(args) -> int:
    config = _load(args)
    conn = storage.connect(config.storage.db_file)
    storage.init_db(conn)
    print(json.dumps(storage.delta(conn, args.since), indent=2, ensure_ascii=False))
    conn.close()
    return 0


def _cmd_dedup(args) -> int:
    # getattr defaults let `run` invoke this without owning the dedup-only flags.
    config = _load(args)
    conn = storage.connect(config.storage.db_file)
    storage.init_db(conn)
    result = storage.run_dedup(
        conn,
        batch_size=getattr(args, "batch_size", 500),
        vacuum=not getattr(args, "no_vacuum", False),
    )
    print(json.dumps(result, indent=2))
    conn.close()
    return 0


def _cmd_report(args) -> int:
    """Write a static HTML analysis report of the corpus. Opens the DB read-only
    (never writes), so it is safe to run at any time -- even while a crawl is in
    progress -- and reflects the last committed state."""
    import sqlite3
    import webbrowser

    from . import dashboard

    config = _load(args)
    db = config.storage.db_file
    if not db.exists():
        raise SystemExit(f"database not found: {db} (run a fetch/extract first).")
    out = Path(args.output) if args.output else db.parent / "analysis.html"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        dashboard.write_report(
            conn,
            sites=config.sites,
            min_words=config.extract.min_words,
            db_path=db,
            out_path=out,
        )
    finally:
        conn.close()
    print(f"wrote {out}")
    print("open it in a browser; to refresh, re-run this command and reload the page.")
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


def _cmd_backfill_links(args) -> int:
    config = _load(args)
    counts = crawl.backfill_links(config)
    print(" ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


def _cmd_reset_site(args) -> int:
    config = _load(args)
    sites = _resolve_sites(config, args.site)
    conn = storage.connect(config.storage.db_file)
    storage.init_db(conn)
    for s in sites:
        counts = storage.reset_site(conn, s.allowed_domain)
        total = sum(counts.values())
        detail = " ".join(f"{k}={v}" for k, v in counts.items())
        print(f"[{s.name}] reset {s.allowed_domain}: {detail} (deleted {total} rows)")
    conn.close()
    return 0


def _add_crawl_args(p) -> None:
    """Shared fetch-override flags for the ``fetch`` and ``run`` subcommands, kept
    in one place so the two never drift."""
    p.add_argument("--max-pages", type=int, default=None)
    p.add_argument(
        "--site",
        action="append",
        metavar="NAME",
        help="Crawl only this site (config name or allowed_domain); repeatable. "
        "Scopes both sitemap refresh and crawling to the selected site(s).",
    )
    p.add_argument(
        "--max-pages-per-host",
        type=int,
        default=None,
        metavar="N",
        help="Per-hostname page budget for this run (0 = unlimited; overrides "
        "crawl.max_pages_per_host). Backstop against a single runaway subdomain.",
    )
    p.add_argument(
        "--workers-per-host",
        type=int,
        default=None,
        help="Concurrent fetch workers per host (overrides config crawl.workers_per_host).",
    )
    p.add_argument(
        "--request-delay",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Per-host delay between requests, in seconds "
        "(overrides crawl.request_delay_seconds).",
    )
    recheck = p.add_mutually_exclusive_group()
    recheck.add_argument(
        "--changed-only",
        action="store_true",
        help="Only re-check sitemap-advertised changed URLs (recheck=changed-only).",
    )
    recheck.add_argument(
        "--new-only",
        action="store_true",
        help="Only fetch queued URLs never fetched before; never re-download "
        "already-stored pages (recheck=new-only).",
    )
    recheck.add_argument(
        "--full",
        action="store_true",
        help="Re-check all present URLs and ignore stored validators (force full re-download).",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dhbw-scraper", description="Incremental DHBW dual-site scraper."
    )
    parser.add_argument("--config", default=None, help="Path to config.toml.")
    sub = parser.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch", help="Phase 1: crawl + download.")
    _add_crawl_args(f)
    f.set_defaults(func=_cmd_fetch)

    e = sub.add_parser("extract", help="Phase 2: extract + quality-gate (HTML + PDF).")
    e.add_argument("--workers", type=int, default=None)
    e.set_defaults(func=_cmd_extract)

    eh = sub.add_parser("extract-html", help="Phase 2: extract HTML docs only.")
    eh.add_argument("--workers", type=int, default=None)
    eh.set_defaults(func=_cmd_extract_html)

    ep = sub.add_parser("extract-pdf", help="Phase 2: extract PDF docs only.")
    ep.add_argument("--workers", type=int, default=None)
    ep.set_defaults(func=_cmd_extract_pdf)

    r = sub.add_parser("run", help="fetch then extract.")
    _add_crawl_args(r)
    r.add_argument("--workers", type=int, default=None)
    r.set_defaults(func=_cmd_run)

    s = sub.add_parser("stats", help="Print DB counts.")
    s.set_defaults(func=_cmd_stats)

    rp = sub.add_parser(
        "report",
        help="Write a self-contained HTML analysis report of the corpus (read-only). "
        "Open the file in a browser; re-run + reload to refresh.",
    )
    rp.add_argument(
        "-o",
        "--output",
        default=None,
        metavar="PATH",
        help="Output HTML path (default: <db dir>/analysis.html).",
    )
    rp.add_argument(
        "--open",
        action="store_true",
        help="Open the report in the default browser after writing it.",
    )
    rp.set_defaults(func=_cmd_report)

    rs = sub.add_parser(
        "reset-site",
        help="Delete a site's crawl state (queue/crawl_log/documents/links) so it "
        "re-crawls from scratch. Leaves the content-addressed raw_docs cache intact.",
    )
    rs.add_argument(
        "--site",
        action="append",
        required=True,
        metavar="NAME",
        help="Site to reset (config name or allowed_domain); repeatable.",
    )
    rs.set_defaults(func=_cmd_reset_site)

    dd = sub.add_parser(
        "dedup",
        help="Backfill text_sha256 and hard-delete duplicate documents "
        "(keep the cleanest URL per distinct extracted text).",
    )
    dd.add_argument("--batch-size", type=int, default=500)
    dd.add_argument(
        "--no-vacuum",
        action="store_true",
        help="Skip the reclaiming VACUUM after deleting duplicates.",
    )
    dd.set_defaults(func=_cmd_dedup)

    d = sub.add_parser("delta", help="Emit re-index delta since a timestamp.")
    d.add_argument("--since", required=True)
    d.set_defaults(func=_cmd_delta)

    bl = sub.add_parser(
        "backfill-links",
        help="Rebuild the links edge table from raw HTML already on disk (no "
        "network). Repairs the sparse link graph left by 304-only re-crawls; "
        "additive and idempotent.",
    )
    bl.set_defaults(func=_cmd_backfill_links)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
