"""ONE-TIME backfill: classify every existing document into Standort /
Studienabteilung / Studiengang, populating the enrichment columns that the
Phase-2 write path now fills going forward.

Run once against the current corpus, then `git rm` this file -- the permanent
logic lives in scraper.classify / scraper.storage, which this only drives.

    uv run python scripts/backfill_classification.py            # uses config.toml
    uv run python scripts/backfill_classification.py --config other.toml

Keyset-paginated by documents.id (a forward index range scan, like run_dedup) so
only `batch_size` rows are resident at once. Never touches updated_at."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scraper import storage  # noqa: E402
from scraper.config import load_config  # noqa: E402


def backfill_classification(conn, batch_size: int = 500) -> dict:
    storage.init_db(conn)  # ensure tables/columns/seed exist
    updated = 0
    last_id = ""
    while True:
        rows = conn.execute(
            "SELECT id, url, site, title, text, metadata FROM documents "
            "WHERE id > ? ORDER BY id LIMIT ?",
            (last_id, batch_size),
        ).fetchall()
        if not rows:
            break
        with storage.write_txn(conn):
            for r in rows:
                doc = {
                    "title": r["title"],
                    "text": r["text"] or "",
                    "metadata": json.loads(r["metadata"]) if r["metadata"] else None,
                }
                storage._set_classification(conn, r["id"], r["url"], r["site"], doc)
        updated += len(rows)
        last_id = rows[-1]["id"]
    return {"updated": updated}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--batch-size", type=int, default=None)
    args = parser.parse_args(argv)

    config = load_config(Path(args.config))
    batch = args.batch_size or config.dedup.batch_size
    conn = storage.connect(config.storage.db_file)
    try:
        result = backfill_classification(conn, batch_size=batch)
    finally:
        conn.close()
    print(f"backfill complete: {result['updated']} documents classified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
