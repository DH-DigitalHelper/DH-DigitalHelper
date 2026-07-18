"""Phase 2: extract cached content, quality-gate it, materialize documents."""

from __future__ import annotations

import threading
import time
from concurrent.futures import (
    FIRST_COMPLETED,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

from . import fetch as fetchmod
from . import html_extract, pdf_extract, quality, storage
from .progress import Progress

_MAX_TASKS_PER_CHILD = 200


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _extract_dispatch(source_type: str, raw_path_str: str) -> dict | None:
    """Turn one cached blob into a doc dict, running in a pool worker process."""
    data = Path(raw_path_str).read_bytes()
    if source_type == "html":
        return html_extract.extract_html(data)
    return pdf_extract.extract_pdf(data)


def _materialize(conn, raw_row, doc, accepted, reason, now) -> str:
    """Persist one extracted doc (its raw_docs extract row plus every present URL's document) atomically in one write_txn."""
    digest = raw_row["content_sha256"]
    source_type = raw_row["source_type"]
    try:
        with storage.write_txn(conn):
            storage._save_extraction(
                conn, digest, doc, accepted, None if accepted else reason, None, now
            )
            if accepted:
                for url_row in storage.urls_for_content(conn, digest):
                    storage._upsert_document(
                        conn,
                        url_row["url"],
                        url_row["site"],
                        source_type,
                        digest,
                        doc,
                        now,
                    )
        return "indexed" if accepted else "rejected"
    except Exception as exc:  # noqa: BLE001
        return _record_error(conn, digest, exc, now)


def extract_one(conn, raw_row, config, extractors, now) -> str:
    digest = raw_row["content_sha256"]
    source_type = raw_row["source_type"]

    raw_path = storage.RawCache(config.storage.raw_dir).path_for(
        digest, fetchmod.ext_for(source_type)
    )

    try:
        data = raw_path.read_bytes()
        doc = extractors[source_type](data)
        accepted, reason = quality.evaluate(doc, config.extract.min_words)
    except Exception as exc:  # noqa: BLE001
        return _record_error(conn, digest, exc, now)

    return _materialize(conn, raw_row, doc, accepted, reason, now)


def _record_error(conn, digest, exc, now) -> str:
    try:
        storage.save_extraction(conn, digest, None, False, None, str(exc), now)
    except Exception:  # noqa: BLE001
        pass
    return "error"


def run_extract(
    config, extractors=None, clock=_now, progress=None, source_type=None
) -> dict:
    """Run the extract phase over pending raw_docs."""
    if progress is None:
        progress = Progress()
    counts = {"indexed": 0, "rejected": 0, "error": 0}

    recovery_conn = storage.connect(config.storage.db_file)
    try:
        storage.init_db(recovery_conn)
        storage.reset_extract_in_progress(recovery_conn, source_type)
        storage.reset_extract_errors(recovery_conn, source_type)
    finally:
        recovery_conn.close()

    progress.header("Extracting")
    n_workers = max(1, config.extract.workers)

    if extractors is not None:
        _run_threaded(
            config, extractors, clock, progress, source_type, counts, n_workers
        )
    else:
        _run_pooled(config, clock, progress, source_type, counts, n_workers)

    progress.summary("Extraction complete", counts)
    return counts


def _run_threaded(config, extractors, clock, progress, source_type, counts, n_workers):
    """In-process thread path for injected extractors (tests)."""
    lock = threading.Lock()

    def worker():
        conn = storage.connect(config.storage.db_file)
        try:
            while True:
                row = storage.claim_pending_raw(conn, source_type)
                if row is None:
                    return
                outcome = extract_one(conn, row, config, extractors, clock())
                with lock:
                    counts[outcome] = counts.get(outcome, 0) + 1
                    snapshot = dict(counts)
                queued = storage.count_pending_raw(conn, source_type)
                progress.update(snapshot, row["content_sha256"], queued=queued)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        futures = [ex.submit(worker) for _ in range(n_workers)]
        for f in futures:
            f.result()


def _run_pooled(config, clock, progress, source_type, counts, n_workers):
    """Production path: fan the CPU-bound extract out across worker processes while the parent keeps the single DB connection and does every write."""
    conn = storage.connect(config.storage.db_file)
    raw_cache = storage.RawCache(config.storage.raw_dir)

    initial = storage.count_pending_raw(conn, source_type)
    processed = 0
    if initial == 0:
        conn.close()
        return

    def new_pool():
        return ProcessPoolExecutor(
            max_workers=n_workers, max_tasks_per_child=_MAX_TASKS_PER_CHILD
        )

    def refill(pool, inflight, isolate):
        limit = 1 if isolate else max(2, n_workers * 2)
        while len(inflight) < limit:
            row = storage.claim_pending_raw(conn, source_type)
            if row is None:
                return
            st = row["source_type"]
            path = raw_cache.path_for(row["content_sha256"], fetchmod.ext_for(st))
            inflight[pool.submit(_extract_dispatch, st, str(path))] = row

    pool = new_pool()
    isolate = False
    try:
        inflight: dict = {}
        refill(pool, inflight, isolate)
        while inflight:
            done, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
            broke = False
            for fut in done:
                row = inflight.pop(fut)
                try:
                    outcome = _handle_result(conn, row, fut, config, clock())
                except BrokenProcessPool:
                    broke = True
                    if isolate:
                        _record_error(
                            conn,
                            row["content_sha256"],
                            RuntimeError("worker crashed extracting this blob"),
                            clock(),
                        )
                        counts["error"] = counts.get("error", 0) + 1
                        processed += 1
                    else:
                        storage.requeue_extraction(conn, row["content_sha256"], clock())
                    continue
                counts[outcome] = counts.get(outcome, 0) + 1
                processed += 1
                remaining = max(0, initial - processed)
                progress.update(dict(counts), row["content_sha256"], queued=remaining)
            if broke:
                for r in inflight.values():
                    storage.requeue_extraction(conn, r["content_sha256"], clock())
                inflight.clear()
                pool.shutdown(wait=False, cancel_futures=True)
                pool = new_pool()
                isolate = True
            else:
                isolate = False
            refill(pool, inflight, isolate)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        conn.close()


def _handle_result(conn, row, fut, config, now) -> str:
    """Run the quality gate and persist for one completed pool future."""
    digest = row["content_sha256"]
    try:
        doc = fut.result()
    except BrokenProcessPool:
        raise
    except Exception as exc:  # noqa: BLE001
        return _record_error(conn, digest, exc, now)
    try:
        accepted, reason = quality.evaluate(doc, config.extract.min_words)
    except Exception as exc:  # noqa: BLE001
        return _record_error(conn, digest, exc, now)
    return _materialize(conn, row, doc, accepted, reason, now)
