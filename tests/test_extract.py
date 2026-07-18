import io

import pytest

from scraper import extract, storage as st
from scraper.config import (
    Config,
    CrawlConfig,
    DedupConfig,
    ExtractConfig,
    Site,
    StorageConfig,
)
from scraper.progress import Progress

NOW = "2026-07-14T00:00:00"


def cfg(tmp_path):
    return Config(
        root=tmp_path,
        sites=[Site("dhbw", "https://www.dhbw.de", "www.dhbw.de")],
        crawl=CrawlConfig(
            use_sitemap=True,
            max_pages=0,
            request_delay_seconds=0.0,
            respect_robots=False,
            workers_per_host=1,
            recheck="all",
            user_agent="ua",
        ),
        extract=ExtractConfig(2, 50),
        dedup=DedupConfig(),
        storage=StorageConfig(tmp_path / "db.sqlite3", tmp_path / "raw"),
    )


def write_raw(tmp_path, digest, body, kind="html"):
    """Write body to the cache at digest's path and return that path."""
    cache = st.RawCache(tmp_path / "raw")
    path = cache.path_for(digest, ".html" if kind == "html" else ".pdf")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return cache, path


def test_extract_dispatch_preserves_non_utf8_german_text(tmp_path):
    """A legacy Windows-1252 page must survive extraction with its umlauts."""
    html = (
        '<html><head><meta charset="windows-1252"><title>Pruefung</title></head>'
        "<body><main><p>Die Prüfungsordnung regelt die Fächer und Module "
        "des Studiums ausführlich und verbindlich für alle Studierenden "
        "der Dualen Hochschule Baden-Württemberg.</p></main></body></html>"
    )
    path = tmp_path / "cp1252.html"
    path.write_bytes(html.encode("windows-1252"))

    doc = extract._extract_dispatch("html", str(path))

    assert doc is not None
    assert "�" not in doc["text"], "cp1252 bytes were mangled into replacement chars"
    assert "Prüfungsordnung" in doc["text"]
    assert "Fächer" in doc["text"]
    assert doc["title"] == "Pruefung"


def setup_raw(tmp_path, digest="c1", body=b"<html>x</html>", kind="html"):
    conn = st.connect(":memory:")
    st.init_db(conn)
    cache, path = write_raw(tmp_path, digest, body, kind)
    st.upsert_raw_doc(conn, digest, kind, str(path), len(body), NOW)
    st.enqueue(conn, "https://www.dhbw.de/a", "www.dhbw.de", 0, None, NOW)
    st.mark_url_checked(
        conn, "https://www.dhbw.de/a", 200, None, None, digest, True, True, NOW
    )
    return conn, cache


def good_doc(_bytes):
    text = "This is genuinely useful DHBW content. " * 10
    return {
        "title": "T",
        "text": text,
        "markdown": text,
        "lang": "de",
        "word_count": len(text.split()),
        "metadata": {"x": 1},
    }


def test_extract_one_indexes_present_urls(tmp_path):
    conn, _ = setup_raw(tmp_path)
    row = st.claim_pending_raw(conn)
    outcome = extract.extract_one(
        conn, row, cfg(tmp_path), {"html": good_doc, "pdf": good_doc}, NOW
    )
    assert outcome == "indexed"
    doc = conn.execute(
        "SELECT * FROM documents WHERE url='https://www.dhbw.de/a'"
    ).fetchone()
    assert doc is not None and doc["word_count"] > 50
    assert st.get_url_state(conn, "https://www.dhbw.de/a")


def test_extract_one_ignores_stale_raw_path_from_another_machine(tmp_path):
    """A stale absolute raw_path from another machine is ignored because the content-addressed blob is found via raw_dir + digest."""
    conn, _ = setup_raw(tmp_path)
    conn.execute(
        "UPDATE raw_docs SET raw_path=? WHERE content_sha256='c1'",
        ("/home/someone/elsewhere/data/raw/c1.html",),
    )
    conn.commit()

    row = st.claim_pending_raw(conn)
    outcome = extract.extract_one(
        conn, row, cfg(tmp_path), {"html": good_doc, "pdf": good_doc}, NOW
    )

    assert outcome == "indexed"
    raw = conn.execute("SELECT * FROM raw_docs WHERE content_sha256='c1'").fetchone()
    assert raw["extract_error"] is None


def test_extract_one_rejects_low_quality(tmp_path):
    conn, _ = setup_raw(tmp_path)
    row = st.claim_pending_raw(conn)
    outcome = extract.extract_one(
        conn,
        row,
        cfg(tmp_path),
        {
            "html": lambda b: {
                "text": "too short",
                "markdown": "x",
                "word_count": 2,
                "metadata": None,
            }
        },
        NOW,
    )
    assert outcome == "rejected"
    assert conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"] == 0
    raw = conn.execute("SELECT extract_state, reject_reason FROM raw_docs").fetchone()
    assert raw["extract_state"] == "rejected" and "short" in raw["reject_reason"]


def test_extract_one_dedups_present_urls_sharing_content(tmp_path):
    """Source-1 dedup: several present URLs sharing one content blob collapse on the text hash so only the cleanest URL survives."""
    conn, _ = setup_raw(tmp_path, digest="c1")
    st.enqueue(conn, "https://www.dhbw.de/b", "www.dhbw.de", 0, None, NOW)
    st.mark_url_checked(
        conn, "https://www.dhbw.de/b", 200, None, None, "c1", True, True, NOW
    )
    row = st.claim_pending_raw(conn)
    outcome = extract.extract_one(
        conn, row, cfg(tmp_path), {"html": good_doc, "pdf": good_doc}, NOW
    )
    assert outcome == "indexed"
    urls = {
        r["url"]
        for r in conn.execute("SELECT url FROM documents WHERE present=1").fetchall()
    }
    assert urls == {"https://www.dhbw.de/a"}


def test_extract_dedups_byte_different_urls_with_same_text(tmp_path):
    """Source-2 dedup: two URLs with different raw bytes that extract to identical text collapse to a single document, and the cleanest URL wins."""
    c = cfg(tmp_path)
    object.__setattr__(c.extract, "workers", 1)
    conn = st.connect(c.storage.db_file)
    st.init_db(conn)
    _, p1 = write_raw(tmp_path, "c1", b"<html>one</html>")
    _, p2 = write_raw(tmp_path, "c2", b"<html>two-different-bytes</html>")
    st.upsert_raw_doc(conn, "c1", "html", str(p1), 16, NOW)
    st.upsert_raw_doc(conn, "c2", "html", str(p2), 24, NOW)
    st.enqueue(conn, "https://www.dhbw.de/firmen/", "www.dhbw.de", 0, None, NOW)
    st.mark_url_checked(
        conn, "https://www.dhbw.de/firmen/", 200, None, None, "c1", True, True, NOW
    )
    st.enqueue(conn, "https://www.dhbw.de/firmen/?cHash=x", "www.dhbw.de", 0, None, NOW)
    st.mark_url_checked(
        conn,
        "https://www.dhbw.de/firmen/?cHash=x",
        200,
        None,
        None,
        "c2",
        True,
        True,
        NOW,
    )
    conn.close()

    counts = extract.run_extract(
        c, {"html": good_doc, "pdf": good_doc}, clock=lambda: NOW
    )
    assert counts == {"indexed": 2, "rejected": 0, "error": 0}

    conn2 = st.connect(c.storage.db_file)
    urls = [
        r["url"]
        for r in conn2.execute("SELECT url FROM documents WHERE present=1").fetchall()
    ]
    assert urls == ["https://www.dhbw.de/firmen/"]
    conn2.close()


def test_extract_one_materialization_is_atomic_across_urls(tmp_path, monkeypatch):
    """All of a doc's writes land in one write_txn, so a failure materializing a later shared URL rolls the whole transaction back and yields an error outcome."""
    conn, _ = setup_raw(tmp_path, digest="c1")
    st.enqueue(conn, "https://www.dhbw.de/b", "www.dhbw.de", 0, None, NOW)
    st.mark_url_checked(
        conn, "https://www.dhbw.de/b", 200, None, None, "c1", True, True, NOW
    )
    row = st.claim_pending_raw(conn)

    calls = {"n": 0}
    real_upsert = st._upsert_document

    def flaky_upsert(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom on 2nd url")
        return real_upsert(*args, **kwargs)

    monkeypatch.setattr(st, "_upsert_document", flaky_upsert)

    outcome = extract.extract_one(
        conn, row, cfg(tmp_path), {"html": good_doc, "pdf": good_doc}, NOW
    )
    assert outcome == "error"
    assert conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"] == 0
    raw = conn.execute("SELECT extract_state FROM raw_docs").fetchone()
    assert raw["extract_state"] == "error"


def test_extract_one_records_error_when_extractor_raises(tmp_path):
    conn, _ = setup_raw(tmp_path)
    row = st.claim_pending_raw(conn)

    def boom(_bytes):
        raise ValueError("kaboom")

    outcome = extract.extract_one(conn, row, cfg(tmp_path), {"html": boom}, NOW)
    assert outcome == "error"
    assert conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"] == 0
    raw = conn.execute("SELECT extract_state, extract_error FROM raw_docs").fetchone()
    assert raw["extract_state"] == "error"
    assert "kaboom" in raw["extract_error"]


def test_extract_one_isolates_failures_after_extraction(tmp_path):
    """A crash in materialization after a successful extraction must still yield an error outcome instead of killing the worker thread."""
    conn, _ = setup_raw(tmp_path)
    row = st.claim_pending_raw(conn)

    def missing_markdown(_bytes):
        text = "This is genuinely useful DHBW content. " * 10
        return {"title": "T", "text": text, "word_count": len(text.split())}

    outcome = extract.extract_one(
        conn, row, cfg(tmp_path), {"html": missing_markdown}, NOW
    )
    assert outcome == "error"
    assert conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"] == 0
    raw = conn.execute("SELECT extract_state, extract_error FROM raw_docs").fetchone()
    assert raw["extract_state"] == "error"
    assert raw["extract_error"]


def test_extract_one_populates_classification_columns(tmp_path):
    conn, _ = setup_raw(tmp_path)
    row = st.claim_pending_raw(conn)
    outcome = extract.extract_one(
        conn, row, cfg(tmp_path), {"html": good_doc, "pdf": good_doc}, NOW
    )
    assert outcome == "indexed"
    doc = conn.execute(
        "SELECT standort_id, department_id, classify_meta FROM documents "
        "WHERE url='https://www.dhbw.de/a'"
    ).fetchone()
    standort = conn.execute(
        "SELECT name FROM standorte WHERE id=?", (doc["standort_id"],)
    ).fetchone()
    dept = conn.execute(
        "SELECT name FROM departments WHERE id=?", (doc["department_id"],)
    ).fetchone()
    assert standort["name"] == "dhbw"
    assert dept["name"] == "unknown"
    assert doc["classify_meta"] is not None


def test_run_extract_processes_pending_docs_through_thread_pool(tmp_path):
    c = cfg(tmp_path)
    conn = st.connect(c.storage.db_file)
    st.init_db(conn)
    body = b"<html>x</html>"
    _, path = write_raw(tmp_path, "c1", body)
    st.upsert_raw_doc(conn, "c1", "html", str(path), len(body), NOW)
    st.enqueue(conn, "https://www.dhbw.de/a", "www.dhbw.de", 0, None, NOW)
    st.mark_url_checked(
        conn, "https://www.dhbw.de/a", 200, None, None, "c1", True, True, NOW
    )
    conn.close()

    counts = extract.run_extract(
        c, {"html": good_doc, "pdf": good_doc}, clock=lambda: NOW
    )

    assert counts == {"indexed": 1, "rejected": 0, "error": 0}
    conn2 = st.connect(c.storage.db_file)
    doc = conn2.execute(
        "SELECT * FROM documents WHERE url='https://www.dhbw.de/a'"
    ).fetchone()
    assert doc is not None and doc["word_count"] > 50
    conn2.close()


def test_run_extract_emits_header_and_summary_without_announcing_drops(tmp_path):
    c = cfg(tmp_path)
    conn = st.connect(c.storage.db_file)
    st.init_db(conn)
    good_body = b"<html>good</html>"
    bad_body = b"<html>bad</html>"
    _, good_path = write_raw(tmp_path, "c" * 64, good_body)
    _, bad_path = write_raw(tmp_path, "d" * 64, bad_body)
    st.upsert_raw_doc(conn, "c" * 64, "html", str(good_path), len(good_body), NOW)
    st.upsert_raw_doc(conn, "d" * 64, "html", str(bad_path), len(bad_body), NOW)
    st.enqueue(conn, "https://www.dhbw.de/a", "www.dhbw.de", 0, None, NOW)
    st.mark_url_checked(
        conn, "https://www.dhbw.de/a", 200, None, None, "c" * 64, True, True, NOW
    )
    st.enqueue(conn, "https://www.dhbw.de/b", "www.dhbw.de", 0, None, NOW)
    st.mark_url_checked(
        conn, "https://www.dhbw.de/b", 200, None, None, "d" * 64, True, True, NOW
    )
    conn.close()

    def extractor(data):
        if data == good_body:
            return good_doc(data)
        return {"text": "too short", "markdown": "x", "word_count": 2, "metadata": None}

    buf = io.StringIO()
    progress = Progress(stream=buf, is_tty=False)

    counts = extract.run_extract(
        c, {"html": extractor, "pdf": extractor}, clock=lambda: NOW, progress=progress
    )

    assert counts == {"indexed": 1, "rejected": 1, "error": 0}
    out = buf.getvalue()
    assert "Extracting" in out
    assert "Extraction complete" in out
    assert "dropped" not in out


def test_run_extract_recovers_stranded_in_progress_raw_doc(tmp_path):
    """A raw_doc left at extract_state='in_progress' by a crashed worker must not be stranded forever, since run_extract resets stray in_progress rows to pending before spawning workers."""
    c = cfg(tmp_path)
    conn = st.connect(c.storage.db_file)
    st.init_db(conn)
    body = b"<html>x</html>"
    _, path = write_raw(tmp_path, "c1", body)
    st.upsert_raw_doc(conn, "c1", "html", str(path), len(body), NOW)
    st.enqueue(conn, "https://www.dhbw.de/a", "www.dhbw.de", 0, None, NOW)
    st.mark_url_checked(
        conn, "https://www.dhbw.de/a", 200, None, None, "c1", True, True, NOW
    )
    claimed = st.claim_pending_raw(conn)
    assert claimed["content_sha256"] == "c1"
    row = conn.execute(
        "SELECT extract_state FROM raw_docs WHERE content_sha256='c1'"
    ).fetchone()
    assert row["extract_state"] == "in_progress"
    conn.close()

    counts = extract.run_extract(
        c, {"html": good_doc, "pdf": good_doc}, clock=lambda: NOW
    )

    assert counts == {"indexed": 1, "rejected": 0, "error": 0}
    conn2 = st.connect(c.storage.db_file)
    row2 = conn2.execute(
        "SELECT extract_state FROM raw_docs WHERE content_sha256='c1'"
    ).fetchone()
    assert row2["extract_state"] == "done"
    doc = conn2.execute(
        "SELECT * FROM documents WHERE url='https://www.dhbw.de/a'"
    ).fetchone()
    assert doc is not None
    conn2.close()


def test_run_extract_retries_errored_raw_doc(tmp_path):
    """A raw_doc recorded as extract_state='error' must be retried on the next run, and the stale extract_error is cleared once it succeeds."""
    c = cfg(tmp_path)
    conn = st.connect(c.storage.db_file)
    st.init_db(conn)
    body = b"<html>x</html>"
    _, path = write_raw(tmp_path, "c1", body)
    st.upsert_raw_doc(conn, "c1", "html", str(path), len(body), NOW)
    st.enqueue(conn, "https://www.dhbw.de/a", "www.dhbw.de", 0, None, NOW)
    st.mark_url_checked(
        conn, "https://www.dhbw.de/a", 200, None, None, "c1", True, True, NOW
    )
    st.save_extraction(conn, "c1", None, False, None, "boom", NOW)
    row = conn.execute(
        "SELECT extract_state, extract_error FROM raw_docs WHERE content_sha256='c1'"
    ).fetchone()
    assert row["extract_state"] == "error"
    assert row["extract_error"] == "boom"
    conn.close()

    counts = extract.run_extract(
        c, {"html": good_doc, "pdf": good_doc}, clock=lambda: NOW
    )

    assert counts == {"indexed": 1, "rejected": 0, "error": 0}
    conn2 = st.connect(c.storage.db_file)
    row2 = conn2.execute(
        "SELECT extract_state, extract_error FROM raw_docs WHERE content_sha256='c1'"
    ).fetchone()
    assert row2["extract_state"] == "done"
    assert row2["extract_error"] is None
    doc = conn2.execute(
        "SELECT * FROM documents WHERE url='https://www.dhbw.de/a'"
    ).fetchone()
    assert doc is not None
    conn2.close()


def _seed_two_types(tmp_path):
    """One pending html raw_doc + one pending pdf raw_doc."""
    conn = st.connect(str(tmp_path / "db.sqlite3"))
    st.init_db(conn)
    _, hp = write_raw(tmp_path, "h" * 64, b"<html>x</html>", kind="html")
    _, pp = write_raw(tmp_path, "p" * 64, b"%PDF-1.4", kind="pdf")
    st.upsert_raw_doc(conn, "h" * 64, "html", str(hp), 14, NOW)
    st.upsert_raw_doc(conn, "p" * 64, "pdf", str(pp), 8, NOW)
    return conn


def test_claim_pending_raw_filters_by_source_type(tmp_path):
    conn = _seed_two_types(tmp_path)
    row = st.claim_pending_raw(conn, source_type="pdf")
    assert row["content_sha256"] == "p" * 64
    assert row["source_type"] == "pdf"
    assert st.claim_pending_raw(conn, source_type="pdf") is None
    assert st.claim_pending_raw(conn, source_type="html")["source_type"] == "html"


def test_count_pending_raw_filters_by_source_type(tmp_path):
    conn = _seed_two_types(tmp_path)
    assert st.count_pending_raw(conn) == 2
    assert st.count_pending_raw(conn, source_type="html") == 1
    assert st.count_pending_raw(conn, source_type="pdf") == 1


def test_reset_extract_in_progress_is_scoped_to_source_type(tmp_path):
    conn = _seed_two_types(tmp_path)
    st.claim_pending_raw(conn, source_type="html")
    st.claim_pending_raw(conn, source_type="pdf")
    reset = st.reset_extract_in_progress(conn, source_type="html")
    assert reset == 1
    states = {
        r["source_type"]: r["extract_state"]
        for r in conn.execute(
            "SELECT source_type, extract_state FROM raw_docs"
        ).fetchall()
    }
    assert states == {"html": "pending", "pdf": "in_progress"}


def test_reset_extract_errors_is_scoped_to_source_type(tmp_path):
    conn = _seed_two_types(tmp_path)
    st.save_extraction(conn, "h" * 64, None, False, None, "boom", NOW)
    st.save_extraction(conn, "p" * 64, None, False, None, "boom", NOW)
    reset = st.reset_extract_errors(conn, source_type="html")
    assert reset == 1
    states = {
        r["source_type"]: r["extract_state"]
        for r in conn.execute(
            "SELECT source_type, extract_state FROM raw_docs"
        ).fetchall()
    }
    assert states == {"html": "pending", "pdf": "error"}


def test_run_extract_pooled_path_indexes_real_html(tmp_path):
    """End-to-end production path: extractors=None spawns a ProcessPoolExecutor and runs the real trafilatura extractor on an html blob scoped to source_type='html'."""
    c = cfg(tmp_path)
    conn = st.connect(c.storage.db_file)
    st.init_db(conn)
    body = (
        b"<html><head><title>DHBW</title></head><body><article>"
        b"<h1>Studium an der DHBW</h1><p>"
        + (b"Das duale Studium verbindet Theorie und Praxis fuer die Studierenden. ")
        * 12
        + b"</p></article></body></html>"
    )
    _, path = write_raw(tmp_path, "c1", body, kind="html")
    st.upsert_raw_doc(conn, "c1", "html", str(path), len(body), NOW)
    st.enqueue(conn, "https://www.dhbw.de/a", "www.dhbw.de", 0, None, NOW)
    st.mark_url_checked(
        conn, "https://www.dhbw.de/a", 200, None, None, "c1", True, True, NOW
    )
    conn.close()

    counts = extract.run_extract(c, clock=lambda: NOW, source_type="html")

    assert counts == {"indexed": 1, "rejected": 0, "error": 0}
    conn2 = st.connect(c.storage.db_file)
    doc = conn2.execute(
        "SELECT * FROM documents WHERE url='https://www.dhbw.de/a'"
    ).fetchone()
    assert doc is not None and doc["word_count"] > 50
    raw = conn2.execute(
        "SELECT extract_state FROM raw_docs WHERE content_sha256='c1'"
    ).fetchone()
    assert raw["extract_state"] == "done"
    conn2.close()


def test_run_extract_fails_fast_on_worker_exception(tmp_path, monkeypatch):
    """A worker-level call that raises must not be silently swallowed; run_extract collects futures and calls .result() so the exception surfaces."""
    c = cfg(tmp_path)
    conn = st.connect(c.storage.db_file)
    st.init_db(conn)
    body = b"<html>x</html>"
    _, path = write_raw(tmp_path, "c1", body)
    st.upsert_raw_doc(conn, "c1", "html", str(path), len(body), NOW)
    st.enqueue(conn, "https://www.dhbw.de/a", "www.dhbw.de", 0, None, NOW)
    st.mark_url_checked(
        conn, "https://www.dhbw.de/a", 200, None, None, "c1", True, True, NOW
    )
    conn.close()

    class _Boom(RuntimeError):
        pass

    def boom_claim(_conn, source_type=None):
        raise _Boom("simulated worker-level failure")

    monkeypatch.setattr(st, "claim_pending_raw", boom_claim)

    with pytest.raises(_Boom):
        extract.run_extract(c, {"html": good_doc, "pdf": good_doc}, clock=lambda: NOW)
