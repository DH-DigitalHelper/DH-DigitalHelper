import sqlite3

import pytest

from scraper import storage as st

NOW = "2026-07-14T00:00:00"


def test_retry_locked_retries_then_succeeds():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    slept = []
    assert st._retry_locked(fn, retries=5, sleep=slept.append) == "ok"
    assert calls["n"] == 3
    assert len(slept) == 2


def test_retry_locked_reraises_locked_after_exhausting_retries():
    def fn():
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        st._retry_locked(fn, retries=2, sleep=lambda _s: None)


def test_retry_locked_does_not_retry_non_lock_errors():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: queue")

    with pytest.raises(sqlite3.OperationalError):
        st._retry_locked(fn, retries=5, sleep=lambda _s: None)
    assert calls["n"] == 1


def test_requeue_url_flips_in_progress_back_to_pending():
    conn = mem()
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW)
    st.claim_pending_url(conn, "x")
    st.requeue_url(conn, "https://x/a")
    assert st.get_url_state(conn, "https://x/a")["work_state"] == "pending"
    assert st.count_pending(conn) == 1


def test_reset_site_deletes_only_target_site_and_keeps_raw_docs():
    conn = mem()
    st.enqueue(conn, "https://a/1", "a.de", 0, None, NOW)
    st.enqueue(conn, "https://a/2", "a.de", 1, None, NOW)
    st.enqueue(conn, "https://b/1", "b.de", 0, None, NOW)
    st.record_fetch(
        conn,
        "run1",
        "https://a/1",
        "https://a/1",
        "a.de",
        200,
        "text/html",
        "h1",
        10,
        "html",
        "new",
        None,
        NOW,
    )
    st.record_fetch(
        conn,
        "run1",
        "https://b/1",
        "https://b/1",
        "b.de",
        200,
        "text/html",
        "h2",
        10,
        "html",
        "new",
        None,
        NOW,
    )
    st.upsert_document(
        conn,
        "https://a/1",
        "a.de",
        "html",
        "h1",
        {"title": "A", "text": "alpha " * 60, "markdown": "mdA", "word_count": 60},
        NOW,
    )
    st.upsert_document(
        conn,
        "https://b/1",
        "b.de",
        "html",
        "h2",
        {"title": "B", "text": "beta " * 60, "markdown": "mdB", "word_count": 60},
        NOW,
    )

    def _uid(u):
        conn.execute("INSERT OR IGNORE INTO urls(url) VALUES (?)", (u,))
        return conn.execute("SELECT id FROM urls WHERE url=?", (u,)).fetchone()[0]

    with st.write_txn(conn):
        conn.execute(
            "INSERT INTO links (src_id, dst_id, site, in_domain, depth, first_seen_at)"
            " VALUES (?,?,?,?,?,?)",
            (_uid("https://a/1"), _uid("https://a/2"), "a.de", 1, 1, NOW),
        )
        conn.execute(
            "INSERT INTO links (src_id, dst_id, site, in_domain, depth, first_seen_at)"
            " VALUES (?,?,?,?,?,?)",
            (_uid("https://b/1"), _uid("https://b/2"), "b.de", 1, 1, NOW),
        )
    st.upsert_raw_doc(conn, "h1", "html", "/raw/h1.html", 10, NOW)

    counts = st.reset_site(conn, "a.de")

    assert counts == {"queue": 2, "crawl_log": 1, "documents": 1, "links": 1}

    def scalar(q):
        return conn.execute(q).fetchone()[0]

    assert scalar("SELECT COUNT(*) FROM queue WHERE site='a.de'") == 0
    assert scalar("SELECT COUNT(*) FROM queue WHERE site='b.de'") == 1
    assert scalar("SELECT COUNT(*) FROM crawl_log WHERE site='b.de'") == 1
    assert scalar("SELECT COUNT(*) FROM documents WHERE site='b.de'") == 1
    assert scalar("SELECT COUNT(*) FROM links WHERE site='b.de'") == 1
    assert scalar("SELECT COUNT(*) FROM raw_docs") == 1


def mem():
    conn = st.connect(":memory:")
    st.init_db(conn)
    return conn


def test_connect_uses_wal_friendly_pragmas():
    conn = st.connect(":memory:")
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 15000


def test_enqueue_dedupes_by_url():
    conn = mem()
    assert st.enqueue(conn, "https://x/a", "x", 0, None, NOW) is True
    assert st.enqueue(conn, "https://x/a", "x", 1, "https://x/b", NOW) is False
    assert st.count_pending(conn) == 1


def test_enqueue_many_inserts_all_and_dedupes():
    conn = mem()
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW)
    rows = [
        ("https://x/a", "x", 1, "https://x/seed", NOW),
        ("https://x/b", "x", 1, "https://x/seed", NOW),
        ("https://x/c", "x", 1, "https://x/seed", NOW),
        ("https://x/b", "x", 2, "https://x/seed", NOW),
    ]
    added = st.enqueue_many(conn, rows)
    assert added == 2
    assert st.count_pending(conn) == 3
    assert st.get_url_state(conn, "https://x/a")["depth"] == 0


def test_write_txn_commits_all_writes_together():
    conn = mem()
    with st.write_txn(conn):
        st._enqueue_many(conn, [("https://x/a", "x", 0, None, NOW)])
        st._enqueue_many(conn, [("https://x/b", "x", 0, None, NOW)])
    assert st.count_pending(conn) == 2


def test_write_txn_rolls_back_all_writes_on_error():
    conn = mem()
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW)
    with pytest.raises(RuntimeError):
        with st.write_txn(conn):
            st._enqueue_many(conn, [("https://x/b", "x", 1, "https://x/a", NOW)])
            raise RuntimeError("boom")
    assert st.get_url_state(conn, "https://x/b") is None
    assert st.count_pending(conn) == 1


def test_claim_uses_composite_index_not_a_scan():
    conn = mem()
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM queue "
        "WHERE site = ? AND work_state = 'pending' ORDER BY depth, url LIMIT 1",
        ("x",),
    ).fetchall()
    detail = " ".join(row["detail"] for row in plan)
    assert "idx_queue_claim" in detail
    assert "SCAN" not in detail


def test_claim_pending_is_atomic_and_marks_in_progress():
    conn = mem()
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW)
    row = st.claim_pending_url(conn, "x")
    assert row["url"] == "https://x/a"
    assert st.claim_pending_url(conn, "x") is None
    assert st.count_pending(conn) == 0


def test_reset_in_progress_requeues():
    conn = mem()
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW)
    st.claim_pending_url(conn, "x")
    assert st.reset_in_progress(conn) == 1
    assert st.count_pending(conn) == 1


def test_set_sitemap_lastmod_requeues_on_advance():
    conn = mem()
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW)
    st.claim_pending_url(conn, "x")
    st.set_sitemap_lastmod(conn, "https://x/a", "x", "2026-01-01", NOW)
    assert st.count_pending(conn) == 0
    st.set_sitemap_lastmod(conn, "https://x/a", "x", "2026-02-01", NOW)
    assert st.count_pending(conn) == 1
    st.set_sitemap_lastmod(conn, "https://x/b", "x", "2026-02-01", NOW)
    assert st.count_pending(conn) == 2


def test_set_sitemap_lastmod_none_does_not_erase_baseline():
    conn = mem()
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW)
    st.claim_pending_url(conn, "x")
    st.set_sitemap_lastmod(conn, "https://x/a", "x", "2026-01-01", NOW)
    assert st.count_pending(conn) == 0
    row = st.get_url_state(conn, "https://x/a")
    assert row["sitemap_lastmod"] == "2026-01-01"
    st.set_sitemap_lastmod(conn, "https://x/a", "x", None, NOW)
    assert st.count_pending(conn) == 0
    row = st.get_url_state(conn, "https://x/a")
    assert row["sitemap_lastmod"] == "2026-01-01"
    st.set_sitemap_lastmod(conn, "https://x/a", "x", "2026-03-01", NOW)
    assert st.count_pending(conn) == 1
    row = st.get_url_state(conn, "https://x/a")
    assert row["sitemap_lastmod"] == "2026-03-01"


def test_requeue_present_urls_flips_only_present_done_rows():
    conn = mem()
    for u in ("a", "b", "c", "d"):
        st.enqueue(conn, f"https://x/{u}", "x", 0, None, NOW)
    st.mark_url_checked(conn, "https://x/a", 200, None, None, "h1", False, True, NOW)
    claimed = st.claim_pending_url(conn, "x")
    assert claimed["url"] == "https://x/b"
    st.mark_url_error(conn, "https://x/c", 500, NOW)
    st.mark_url_removed(conn, "https://x/d", NOW)

    count = st.requeue_present_urls(conn, "x")

    assert count == 1
    assert st.get_url_state(conn, "https://x/a")["work_state"] == "pending"
    assert st.get_url_state(conn, "https://x/b")["work_state"] == "in_progress"
    assert st.get_url_state(conn, "https://x/c")["work_state"] == "error"
    assert st.get_url_state(conn, "https://x/d")["work_state"] == "done"


def test_requeue_present_urls_only_affects_matching_site():
    conn = mem()
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW)
    st.enqueue(conn, "https://y/a", "y", 0, None, NOW)
    st.mark_url_checked(conn, "https://x/a", 200, None, None, "h1", False, True, NOW)
    st.mark_url_checked(conn, "https://y/a", 200, None, None, "h1", False, True, NOW)

    count = st.requeue_present_urls(conn, "x")

    assert count == 1
    assert st.get_url_state(conn, "https://x/a")["work_state"] == "pending"
    assert st.get_url_state(conn, "https://y/a")["work_state"] == "done"


def test_requeue_transient_errors_flips_only_transient_error_rows():
    conn = mem()
    transient = (0, 408, 429, 500, 503, 599)
    for status in transient:
        u = f"https://x/t{status}"
        st.enqueue(conn, u, "x", 0, None, NOW)
        st.mark_url_error(conn, u, status, NOW)
    permanent = (400, 401, 403, 405, 451)
    for status in permanent:
        u = f"https://x/p{status}"
        st.enqueue(conn, u, "x", 0, None, NOW)
        st.mark_url_error(conn, u, status, NOW)
    st.enqueue(conn, "https://x/done", "x", 0, None, NOW)
    st.mark_url_checked(conn, "https://x/done", 200, None, None, "h1", False, True, NOW)
    st.enqueue(conn, "https://y/t503", "y", 0, None, NOW)
    st.mark_url_error(conn, "https://y/t503", 503, NOW)

    count = st.requeue_transient_errors(conn, "x")

    assert count == len(transient)
    for status in transient:
        assert st.get_url_state(conn, f"https://x/t{status}")["work_state"] == "pending"
    for status in permanent:
        assert st.get_url_state(conn, f"https://x/p{status}")["work_state"] == "error"
    assert st.get_url_state(conn, "https://x/done")["work_state"] == "done"
    assert st.get_url_state(conn, "https://y/t503")["work_state"] == "error"


def test_set_sitemap_lastmod_lower_value_does_not_overwrite_or_requeue():
    conn = mem()
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW)
    st.claim_pending_url(conn, "x")
    st.set_sitemap_lastmod(conn, "https://x/a", "x", "2026-01-01", NOW)
    assert st.count_pending(conn) == 0
    st.set_sitemap_lastmod(conn, "https://x/a", "x", "2025-06-01", NOW)
    assert st.count_pending(conn) == 0
    row = st.get_url_state(conn, "https://x/a")
    assert row["sitemap_lastmod"] == "2026-01-01"


def test_claim_only_new_skips_already_fetched_pending_rows():
    conn = mem()
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW)
    conn.execute(
        "UPDATE queue SET last_checked_at = ? WHERE url = ?", (NOW, "https://x/a")
    )
    conn.commit()
    st.enqueue(conn, "https://x/b", "x", 0, None, NOW)

    row = st.claim_pending_url(conn, "x", only_new=True)
    assert row["url"] == "https://x/b"
    assert st.claim_pending_url(conn, "x", only_new=True) is None
    assert st.get_url_state(conn, "https://x/a")["work_state"] == "pending"


def test_claim_default_still_claims_already_fetched_pending_rows():
    conn = mem()
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW)
    conn.execute(
        "UPDATE queue SET last_checked_at = ? WHERE url = ?", (NOW, "https://x/a")
    )
    conn.commit()
    assert st.claim_pending_url(conn, "x")["url"] == "https://x/a"


def test_count_pending_only_new_counts_never_fetched():
    conn = mem()
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW)
    conn.execute(
        "UPDATE queue SET last_checked_at = ? WHERE url = ?", (NOW, "https://x/a")
    )
    conn.commit()
    st.enqueue(conn, "https://x/b", "x", 0, None, NOW)
    assert st.count_pending(conn) == 2
    assert st.count_pending(conn, only_new=True) == 1
    assert st.count_pending(conn, "x", only_new=True) == 1
