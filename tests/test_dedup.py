"""Tests for the `dedup` backfill + maintenance pass (storage.run_dedup).

These seed the `documents` table with legacy rows whose text_sha256 is NULL (as
a pre-dedup DB would have after the migration), then exercise the two phases:
backfill the hash, and hard-delete all but the cleanest URL of each text group.
"""

from dhbw_scraper import storage as st

NOW1 = "2026-07-14T00:00:00"


def db(tmp_path):
    conn = st.connect(str(tmp_path / "dedup.sqlite3"))
    st.init_db(conn)
    return conn


def insert_legacy(conn, url, text, content_sha256, now=NOW1, present=1):
    """Insert a document row with text_sha256 left NULL, mimicking a row that
    predates the dedup column."""
    conn.execute(
        """INSERT INTO documents (id, url, final_url, site, source_type,
               content_sha256, title, text, markdown, lang, word_count, metadata,
               text_sha256, present, revision, first_indexed_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,1,?,?)""",
        (
            st._doc_id(url),
            url,
            url,
            "x",
            "html",
            content_sha256,
            "T",
            text,
            text,
            "en",
            len(text.split()),
            None,
            present,
            now,
            now,
        ),
    )
    conn.commit()


def test_backfill_populates_null_text_sha256(tmp_path):
    conn = db(tmp_path)
    insert_legacy(conn, "https://x/a", "alpha text", "c1")
    insert_legacy(conn, "https://x/b", "beta text", "c2")

    result = st.run_dedup(conn)

    assert result["backfilled"] == 2
    assert result["deleted"] == 0
    rows = conn.execute("SELECT url, text_sha256, text FROM documents").fetchall()
    for r in rows:
        assert r["text_sha256"] == st.text_hash(r["text"])


def test_dedup_deletes_all_but_cleanest(tmp_path):
    conn = db(tmp_path)
    base = "https://x/dualis-firmenliste/"
    # three URLs, identical text, byte-different (distinct content_sha256)
    insert_legacy(conn, base, "same body text", "c1")
    insert_legacy(conn, base + "?cHash=aaa", "same body text", "c2")
    insert_legacy(conn, base + "?tx=1&cHash=bbb", "same body text", "c3")

    result = st.run_dedup(conn)

    assert result["groups"] == 1
    assert result["deleted"] == 2
    assert result["before"] == 3 and result["after"] == 1
    urls = [r["url"] for r in conn.execute("SELECT url FROM documents").fetchall()]
    assert urls == [base]  # the query-param-free URL is the canonical survivor


def test_dedup_is_idempotent(tmp_path):
    conn = db(tmp_path)
    insert_legacy(conn, "https://x/a", "dup", "c1")
    insert_legacy(conn, "https://x/a2", "dup", "c2")
    insert_legacy(conn, "https://x/unique", "solo", "c3")

    first = st.run_dedup(conn)
    assert first["deleted"] == 1

    second = st.run_dedup(conn)
    assert second == {
        "backfilled": 0,
        "groups": 0,
        "deleted": 0,
        "before": 2,
        "after": 2,
    }


def test_backfill_streams_over_multiple_batches(tmp_path):
    conn = db(tmp_path)
    for i in range(5):
        insert_legacy(conn, f"https://x/{i}", f"unique text {i}", f"c{i}")

    result = st.run_dedup(conn, batch_size=2)  # 5 rows over batches of 2

    assert result["backfilled"] == 5
    assert result["deleted"] == 0
    missing = conn.execute(
        "SELECT COUNT(*) c FROM documents WHERE text_sha256 IS NULL"
    ).fetchone()["c"]
    assert missing == 0


def test_dedup_does_not_bump_updated_at(tmp_path):
    conn = db(tmp_path)
    insert_legacy(conn, "https://x/a", "kept text", "c1", now=NOW1)

    st.run_dedup(conn)

    row = conn.execute("SELECT updated_at FROM documents WHERE url='https://x/a'").fetchone()
    assert row["updated_at"] == NOW1  # backfill is metadata-only, never a content change


def test_dedup_keeps_cleanest_across_present_only(tmp_path):
    """A removed (present=0) sibling is never chosen as canonical, and present=0
    tombstones are left untouched by the delete."""
    conn = db(tmp_path)
    insert_legacy(conn, "https://x/gone", "shared", "c1", present=0)
    insert_legacy(conn, "https://x/live/?cHash=z", "shared", "c2", present=1)

    result = st.run_dedup(conn)

    # nothing to delete among present rows (only one present row for that text)
    assert result["deleted"] == 0
    present = {
        r["url"]
        for r in conn.execute("SELECT url FROM documents WHERE present=1").fetchall()
    }
    assert present == {"https://x/live/?cHash=z"}
    # the tombstone still exists (carries the deletion signal)
    assert conn.execute(
        "SELECT COUNT(*) c FROM documents WHERE present=0"
    ).fetchone()["c"] == 1
