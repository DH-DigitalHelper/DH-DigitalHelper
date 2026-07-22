"""Command-line entrypoint for the scraper and downstream preparation steps."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import chunk, crawl, embedding, extract, storage
from .config import load_config


def _run_id() -> str:
    return time.strftime("run-%Y%m%dT%H%M%S", time.gmtime())


def _load(args):
    return load_config(Path(args.config) if args.config else None)


def _resolve_sites(config, names):
    """Filter config.sites to those whose name or allowed_domain is in names."""
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
    results = crawl.run_fetch(config, _run_id())
    for site, counts in results.items():
        print(f"[{site}] " + " ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


def _run_extract(args, source_type) -> int:
    config = _load(args)
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
    config = _load(args)
    conn = storage.connect(config.storage.db_file)
    storage.init_db(conn)
    result = storage.run_dedup(
        conn,
        batch_size=config.dedup.batch_size,
        vacuum=config.dedup.vacuum,
    )
    print(json.dumps(result, indent=2))
    conn.close()
    return 0


def _cmd_reclassify(args) -> int:
    config = _load(args)
    conn = storage.connect(config.storage.db_file)
    storage.init_db(conn)
    result = storage.run_reclassify(conn, batch_size=config.dedup.batch_size)
    print(json.dumps(result, indent=2))
    conn.close()
    return 0


def _cmd_backfill(args) -> int:
    config = _load(args)
    conn = storage.connect(config.storage.db_file)
    storage.init_db(conn)
    result = storage.run_backfill(
        conn, config.storage.raw_dir, batch_size=config.dedup.batch_size
    )
    print(json.dumps(result, indent=2))
    conn.close()
    return 0


def _cmd_chunk(args) -> int:
    config = _load(args)
    conn = storage.connect(config.storage.db_file)
    storage.init_db(conn)
    result = chunk.run_chunking(
        conn,
        target_words=config.chunk.target_words,
        overlap_words=config.chunk.overlap_words,
        batch_size=config.chunk.batch_size,
    )
    print(json.dumps(result, indent=2))
    conn.close()
    return 0


def _cmd_embedding_smoke(args) -> int:
    config = _load(args)
    device = args.device or config.embedding.device
    batch_size = (
        config.embedding.gpu_batch_size
        if device == "cuda"
        else config.embedding.cpu_batch_size
    )
    result = embedding.run_embedding_smoke(
        config.storage.db_file,
        model_name=config.embedding.model,
        device=device,
        batch_size=batch_size,
        cache_dir=config.embedding.cache_dir,
        limit=args.limit,
    )
    preview = result.pop("preview")
    print("Embedding preview:")
    print(f"  chunk_id: {preview['chunk_id']}")
    print(f"  text: {preview['text']}")
    print(f"  first 10 values: {preview['embedding_first_10']}")
    print()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _cmd_index(args) -> int:
    """Refresh SQLite chunks, then fully synchronize the configured collection."""
    try:
        from . import chromaDB
    except ImportError:
        print(
            "indexing failed: ChromaDB is not installed; "
            "install `chroma` for local mode or `chroma-client` for server mode.",
            file=sys.stderr,
        )
        return 1

    config = _load(args)
    conn = storage.connect(config.storage.db_file)
    storage.init_db(conn)
    try:
        chunk_result = chunk.run_chunking(
            conn,
            target_words=config.chunk.target_words,
            overlap_words=config.chunk.overlap_words,
            batch_size=config.chunk.batch_size,
        )
    finally:
        conn.close()

    device = args.device or config.embedding.device
    batch_size = (
        config.embedding.gpu_batch_size
        if device == "cuda"
        else config.embedding.cpu_batch_size
    )
    try:
        client = chromaDB.create_client(
            mode=config.chroma.mode,
            host=config.chroma.host,
            port=config.chroma.port,
            path=str(config.chroma.path),
        )
        collection = chromaDB.get_collection(client, config.chroma.collection)
        index_result = chromaDB.index_chunks(
            collection,
            config.storage.db_file,
            model_name=config.embedding.model,
            device=device,
            batch_size=batch_size,
            cache_dir=config.embedding.cache_dir,
        )
    except embedding.EmbeddingError:
        raise
    except Exception as exc:
        print(f"indexing failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"chunking": chunk_result, "chroma": index_result}, indent=2))
    return 0


def _cmd_report(args) -> int:
    """Write a static HTML analysis report of the corpus."""
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
        out, graph = dashboard.write_report(
            conn,
            sites=config.sites,
            min_words=config.extract.min_words,
            db_path=db,
            out_path=out,
        )
    finally:
        conn.close()
    print(f"wrote {out}")
    print(f"wrote {graph} (interactive crawl discovery tree, linked from the report)")
    print("open it in a browser; to refresh, re-run this command and reload the page.")
    if args.open:
        webbrowser.open(out.resolve().as_uri())
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


def _add_site_arg(p) -> None:
    """The one flag fetch and run share, kept in one place so the two never drift."""
    p.add_argument(
        "--site",
        action="append",
        metavar="NAME",
        help="Crawl only this site (config name or allowed_domain); repeatable. "
        "Scopes both sitemap refresh and crawling to the selected site(s).",
    )


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dhbw-scraper", description="Incremental DHBW dual-site scraper."
    )
    parser.add_argument("--config", default=None, help="Path to config.toml.")
    sub = parser.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch", help="Phase 1: crawl + download.")
    _add_site_arg(f)
    f.set_defaults(func=_cmd_fetch)

    e = sub.add_parser("extract", help="Phase 2: extract + quality-gate (HTML + PDF).")
    e.set_defaults(func=_cmd_extract)

    eh = sub.add_parser("extract-html", help="Phase 2: extract HTML docs only.")
    eh.set_defaults(func=_cmd_extract_html)

    ep = sub.add_parser("extract-pdf", help="Phase 2: extract PDF docs only.")
    ep.set_defaults(func=_cmd_extract_pdf)

    r = sub.add_parser("run", help="fetch then extract.")
    _add_site_arg(r)
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
    dd.set_defaults(func=_cmd_dedup)

    rc = sub.add_parser(
        "reclassify",
        help="Re-tag every document's Standort/Studienabteilung/Studiengang after a "
        "taxonomy or CLASSIFY_VERSION change. Idempotent; never touches updated_at. "
        "Do not run while fetch/extract is running.",
    )
    rc.set_defaults(func=_cmd_reclassify)

    bf = sub.add_parser(
        "backfill",
        help="One-time repair of dead metadata (lang / final_url / titles) across "
        "the existing corpus. Idempotent; never touches updated_at. Do not run "
        "while fetch/extract is running.",
    )
    bf.set_defaults(func=_cmd_backfill)

    ch = sub.add_parser(
        "chunk",
        help="Synchronize structure-aware RAG chunks from present documents. "
        "Preserves source metadata and is incremental/idempotent.",
    )
    ch.set_defaults(func=_cmd_chunk)

    smoke = sub.add_parser(
        "embedding-smoke",
        help="Test FastEmbed on a small chunk sample without storing vectors.",
    )
    smoke.add_argument(
        "--device",
        choices=("cpu", "cuda"),
        default=None,
        help="Execution device (default: embedding.device from config.toml).",
    )
    smoke.add_argument(
        "--limit",
        type=_positive_int,
        default=5,
        metavar="N",
        help="Number of chunks to embed and discard (default: 5).",
    )
    smoke.set_defaults(func=_cmd_embedding_smoke)

    index = sub.add_parser(
        "index",
        help="Refresh SQLite chunks and synchronize the configured Chroma collection.",
    )
    index.add_argument(
        "--device",
        choices=("cpu", "cuda"),
        default=None,
        help="Execution device (default: embedding.device from config.toml).",
    )
    index.set_defaults(func=_cmd_index)

    d = sub.add_parser("delta", help="Emit re-index delta since a timestamp.")
    d.add_argument("--since", required=True)
    d.set_defaults(func=_cmd_delta)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except embedding.EmbeddingError as exc:
        print(f"embedding failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
