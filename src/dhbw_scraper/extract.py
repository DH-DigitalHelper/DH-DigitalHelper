"""Phase 2: extract cached content, quality-gate it, materialize documents.

Extraction is CPU-bound pure-Python work (trafilatura for HTML, pymupdf4llm for
PDF). The production path therefore fans the extract call out across a
``ProcessPoolExecutor`` -- one OS process per worker, each with its own GIL --
so throughput scales with cores instead of being serialized by a single GIL.
The DB stays single-writer in the parent process: workers only turn bytes into a
doc dict, and the parent runs the quality gate and all transactional writes.

Tests inject their own (unpicklable, in-process) extractors; that path keeps
using a ``ThreadPoolExecutor`` so the injected callables run in-process. The two
paths share :func:`_materialize` for the atomic write, so both exercise the same
persistence logic.
"""

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

# Recycle each pool worker after this many blobs so a slow memory creep in the
# native PDF/HTML libraries cannot accumulate across a large backlog.
_MAX_TASKS_PER_CHILD = 200

from . import fetch as fetchmod
from . import html_extract, pdf_extract, quality, storage
from .progress import Progress


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _extract_dispatch(source_type: str, raw_path_str: str) -> dict | None:
    """Turn one cached blob into a doc dict. Runs in a pool worker process.

    Kept module-level (and importing only stateless, thread-safe extractors) so
    it is picklable by reference under ``spawn`` on Windows. The worker reads the
    file itself, so only a path crosses the process boundary in and a doc dict
    out -- never multi-megabyte PDF bytes.
    """
    data = Path(raw_path_str).read_bytes()
    if source_type == "html":
        # Hand trafilatura the raw bytes: it sniffs the encoding (BOM, <meta
        # charset>, heuristics). Pre-decoding as UTF-8 with errors="replace"
        # destroyed every umlaut on the legacy cp1252/latin-1 pages this German
        # corpus still contains -- corrupting the indexed text *and* the
        # text_sha256 the corpus dedups on.
        return html_extract.extract_html(data)
    return pdf_extract.extract_pdf(data)


def _materialize(conn, raw_row, doc, accepted, reason, now) -> str:
    """Persist one extracted doc: its raw_docs extract row plus every present
    URL's document, all in ONE ``write_txn`` (one lock acquisition, one fsync,
    all-or-nothing). Shared by the in-process (test) and pooled (production)
    paths. If any upsert raises, the whole doc rolls back and we record the
    error instead of leaving a half-materialized, done-marked row."""
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
    except Exception as exc:  # noqa: BLE001 - one bad doc must not kill the pool
        return _record_error(conn, digest, exc, now)


def extract_one(conn, raw_row, config, extractors, now) -> str:
    digest = raw_row["content_sha256"]
    source_type = raw_row["source_type"]

    # raw_docs.raw_path records an absolute path on whichever machine fetched
    # the bytes, so it does not survive moving the DB between machines. The
    # cache is content-addressed, so derive the location from this run's
    # raw_dir and the digest instead -- that is portable by construction.
    raw_path = storage.RawCache(config.storage.raw_dir).path_for(
        digest, fetchmod.ext_for(source_type)
    )

    # Extraction is the expensive, CPU-bound step and can raise on a bad blob;
    # run it BEFORE opening any transaction so a worker never holds the single
    # WAL write lock while parsing HTML or converting a PDF.
    try:
        data = raw_path.read_bytes()
        doc = extractors[source_type](data)
        accepted, reason = quality.evaluate(doc, config.extract.min_words)
    except Exception as exc:  # noqa: BLE001 - one bad doc must not kill the pool
        return _record_error(conn, digest, exc, now)

    return _materialize(conn, raw_row, doc, accepted, reason, now)


def _record_error(conn, digest, exc, now) -> str:
    try:
        storage.save_extraction(conn, digest, None, False, None, str(exc), now)
    except Exception:  # noqa: BLE001 - recording the error must not kill the pool either
        pass
    return "error"


def run_extract(
    config, extractors=None, clock=_now, progress=None, source_type=None
) -> dict:
    """Run the extract phase over pending raw_docs.

    ``source_type`` scopes the pass to one type ("html" / "pdf") so extract-html
    and extract-pdf can run (and be tuned) independently; ``None`` processes both.
    When ``extractors`` is provided the injected in-process thread path is used
    (tests); otherwise the real ProcessPoolExecutor path runs.
    """
    if progress is None:
        progress = Progress()
    counts = {"indexed": 0, "rejected": 0, "error": 0}

    # Recover raw_docs stranded at extract_state='in_progress' by a crashed or
    # killed worker from a prior run (Ctrl-C, OOM on a big PDF, a claim raising
    # after busy_timeout) before claiming any new work -- otherwise
    # claim_pending_raw only ever sees 'pending' rows and such a blob would never
    # be retried or materialized. The reset is scoped to this pass's source_type
    # so a concurrent pass of the other type keeps its own in_progress claims.
    # init_db is idempotent/safe here and mirrors run_fetch, so a fresh DB
    # returns empty counts instead of erroring on missing tables.
    recovery_conn = storage.connect(config.storage.db_file)
    try:
        storage.init_db(recovery_conn)
        storage.reset_extract_in_progress(recovery_conn, source_type)
    finally:
        recovery_conn.close()

    progress.header("Extracting")
    n_workers = max(1, config.extract.workers)

    if extractors is not None:
        _run_threaded(config, extractors, clock, progress, source_type, counts, n_workers)
    else:
        _run_pooled(config, clock, progress, source_type, counts, n_workers)

    progress.summary("Extraction complete", counts)
    return counts


def _run_threaded(config, extractors, clock, progress, source_type, counts, n_workers):
    """In-process thread path for injected extractors (tests). Kept for the
    injected case only: the callables tests pass are local closures that cannot
    cross a process boundary."""
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
    """Production path: fan the CPU-bound extract out across worker processes
    while the parent keeps the single DB connection and does every write.

    A worker that dies *natively* (a segfault deep in a PDF/HTML C library, not a
    catchable Python exception) breaks the whole pool and fails every in-flight
    future. We treat that as a pool-level event, not a per-doc error: the
    in-flight blobs are requeued (never lost), the pool is rebuilt, and the next
    round runs a single blob at a time so the one blob that actually crashes the
    worker can be pinpointed and quarantined as an error instead of taking the
    rest of the backlog down with it."""
    conn = storage.connect(config.storage.db_file)
    raw_cache = storage.RawCache(config.storage.raw_dir)

    # Exact remaining count without a per-doc COUNT(*): every processed row --
    # indexed, rejected, or error -- consumes one pending row, so the initial
    # pending count minus processed is the live backlog.
    initial = storage.count_pending_raw(conn, source_type)
    processed = 0
    if initial == 0:
        conn.close()
        return

    def new_pool():
        return ProcessPoolExecutor(
            max_workers=n_workers, max_tasks_per_child=_MAX_TASKS_PER_CHILD
        )

    # Normally keep the pool fed a bit past its worker count so a finished worker
    # always has the next blob queued; drop to 1 while isolating a crash so the
    # culprit is unambiguous.
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
                        # Single-blob round: this blob is the one that crashes the
                        # worker -> quarantine it as an error so the run proceeds.
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
                # The break also killed the other still-in-flight blobs; requeue
                # them (they are innocent) and rebuild the pool.
                for r in inflight.values():
                    storage.requeue_extraction(conn, r["content_sha256"], clock())
                inflight.clear()
                pool.shutdown(wait=False, cancel_futures=True)
                pool = new_pool()
                isolate = True  # next round: one blob at a time to find the culprit
            else:
                isolate = False  # clean round -> back to full throughput
            refill(pool, inflight, isolate)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        conn.close()


def _handle_result(conn, row, fut, config, now) -> str:
    """Run the quality gate + persist for one completed pool future. A catchable
    worker exception (e.g. a malformed PDF) is recorded as an error for that one
    blob. A ``BrokenProcessPool`` (native worker death) is NOT swallowed here --
    it propagates so the caller can requeue in-flight work and rebuild the pool."""
    digest = row["content_sha256"]
    try:
        doc = fut.result()
    except BrokenProcessPool:
        raise
    except Exception as exc:  # noqa: BLE001 - one bad doc must not kill the pool
        return _record_error(conn, digest, exc, now)
    try:
        accepted, reason = quality.evaluate(doc, config.extract.min_words)
    except Exception as exc:  # noqa: BLE001
        return _record_error(conn, digest, exc, now)
    return _materialize(conn, row, doc, accepted, reason, now)
