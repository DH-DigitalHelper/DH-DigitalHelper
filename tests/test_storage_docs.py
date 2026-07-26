import threading

from scraper import storage as st

NOW0 = "2026-07-13T00:00:00"
NOW1 = "2026-07-14T00:00:00"
NOW2 = "2026-07-15T00:00:00"
NOW3 = "2026-07-16T00:00:00"


def mem():
    conn = st.connect(":memory:")
    st.init_db(conn)
    return conn


def doc(text="hello world " * 20, md=None):
    return {
        "title": "T",
        "text": text,
        "markdown": md or text,
        "lang": "en",
        "word_count": len(text.split()),
        "metadata": {"k": "v"},
    }


def test_raw_cache_roundtrip(tmp_path):
    cache = st.RawCache(tmp_path)
    digest, path = cache.write(b"abc", ".html")
    assert cache.has(digest, ".html")
    assert path.read_bytes() == b"abc"


def test_upsert_raw_doc_new_then_idempotent():
    conn = mem()
    assert st.upsert_raw_doc(conn, "h1", "html", "/raw/h1.html", 3, NOW1) is True
    assert st.claim_pending_raw(conn)["content_sha256"] == "h1"
    assert st.claim_pending_raw(conn) is None


def test_upsert_raw_doc_same_digest_returns_false_and_keeps_original_row():
    conn = mem()
    assert st.upsert_raw_doc(conn, "h1", "html", "/raw/h1.html", 3, NOW1) is True
    assert st.upsert_raw_doc(conn, "h1", "pdf", "/raw/other.pdf", 999, NOW2) is False
    rows = conn.execute("SELECT * FROM raw_docs WHERE content_sha256='h1'").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["first_seen_at"] == NOW1
    assert row["source_type"] == "html"
    assert row["raw_path"] == "/raw/h1.html"
    assert row["bytes"] == 3


def test_upsert_raw_doc_concurrent_identical_content_no_crash(tmp_path):
    """Two fetch workers downloading byte-identical content concurrently must not crash on the content_sha256 UNIQUE constraint."""
    db_file = tmp_path / "race.db"
    conn = st.connect(str(db_file))
    st.init_db(conn)
    conn.close()

    n_threads = 8
    results = [None] * n_threads
    errors = []
    errors_lock = threading.Lock()

    def worker(i):
        try:
            wconn = st.connect(str(db_file))
            try:
                results[i] = st.upsert_raw_doc(
                    wconn, "race-digest", "html", f"/raw/{i}.html", 42, NOW1
                )
            finally:
                wconn.close()
        except Exception as exc:  # pragma: no cover
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"upsert_raw_doc raised under concurrency: {errors}"

    check_conn = st.connect(str(db_file))
    rows = check_conn.execute(
        "SELECT * FROM raw_docs WHERE content_sha256='race-digest'"
    ).fetchall()
    assert len(rows) == 1

    assert results.count(True) == 1
    assert results.count(False) == n_threads - 1


def test_document_upsert_lifecycle():
    conn = mem()
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW1)
    st.mark_url_checked(conn, "https://x/a", 200, None, None, "c1", True, True, NOW1)
    assert (
        st.upsert_document(conn, "https://x/a", "x", "html", "c1", doc(), NOW1) == "new"
    )
    assert (
        st.upsert_document(conn, "https://x/a", "x", "html", "c1", doc(), NOW1)
        == "unchanged"
    )
    st.mark_url_checked(conn, "https://x/a", 200, None, None, "c2", True, True, NOW2)
    assert (
        st.upsert_document(
            conn, "https://x/a", "x", "html", "c2", doc("new text " * 30), NOW2
        )
        == "changed"
    )
    row = conn.execute("SELECT * FROM documents WHERE url='https://x/a'").fetchone()
    assert row["revision"] == 2 and row["updated_at"] == NOW2 and row["present"] == 1


def test_delta_returns_upserts_and_deletions():
    conn = mem()
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW1)
    st.upsert_document(conn, "https://x/a", "x", "html", "c1", doc(), NOW1)
    st.enqueue(conn, "https://x/b", "x", 0, None, NOW2)
    st.upsert_document(
        conn, "https://x/b", "x", "html", "c9", doc("distinct content " * 20), NOW2
    )
    st.mark_document_removed(conn, "https://x/a", NOW2)

    d = st.delta(conn, since=NOW1)
    up_urls = {u["url"] for u in d["upserts"]}
    del_urls = {u["url"] for u in d["deletions"]}
    assert "https://x/b" in up_urls
    assert "https://x/a" in del_urls


def test_removed_then_reappears_unchanged_resurfaces_in_delta():
    NOW3 = "2026-07-16T00:00:00"
    conn = mem()
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW1)
    st.mark_url_checked(conn, "https://x/a", 200, None, None, "c1", True, True, NOW1)
    st.upsert_document(conn, "https://x/a", "x", "html", "c1", doc(), NOW1)

    st.mark_document_removed(conn, "https://x/a", NOW2)

    d = st.delta(conn, since=NOW1)
    del_urls = {u["url"] for u in d["deletions"]}
    assert "https://x/a" in del_urls

    st.mark_url_checked(conn, "https://x/a", 200, None, None, "c1", False, True, NOW3)
    assert (
        st.upsert_document(conn, "https://x/a", "x", "html", "c1", doc(), NOW3)
        == "unchanged"
    )

    d2 = st.delta(conn, since=NOW2)
    up_urls = {u["url"] for u in d2["upserts"]}
    del_urls2 = {u["url"] for u in d2["deletions"]}
    assert "https://x/a" in up_urls
    assert "https://x/a" not in del_urls2

    row = conn.execute("SELECT * FROM documents WHERE url='https://x/a'").fetchone()
    assert row["present"] == 1


def test_requeue_extraction_resets_state():
    conn = mem()
    assert st.upsert_raw_doc(conn, "h1", "html", "/raw/h1.html", 3, NOW1) is True
    claimed = st.claim_pending_raw(conn)
    assert claimed["content_sha256"] == "h1"
    st.save_extraction(conn, "h1", doc(), True, None, None, NOW1)
    assert st.claim_pending_raw(conn) is None

    assert st.requeue_extraction(conn, "h1", NOW2) is True
    reclaimed = st.claim_pending_raw(conn)
    assert reclaimed is not None
    assert reclaimed["content_sha256"] == "h1"


def test_reset_extract_in_progress_requeues_only_in_progress():
    conn = mem()
    assert st.upsert_raw_doc(conn, "h1", "html", "/raw/h1.html", 3, NOW1) is True
    assert st.upsert_raw_doc(conn, "h2", "html", "/raw/h2.html", 3, NOW1) is True
    assert st.upsert_raw_doc(conn, "h3", "html", "/raw/h3.html", 3, NOW1) is True
    assert st.upsert_raw_doc(conn, "h4", "html", "/raw/h4.html", 3, NOW1) is True

    claimed = st.claim_pending_raw(conn)
    assert claimed["content_sha256"] == "h1"
    st.save_extraction(conn, "h2", doc(), True, None, None, NOW1)
    st.save_extraction(conn, "h3", doc(), False, "too_short", None, NOW1)
    st.save_extraction(conn, "h4", None, False, None, "boom", NOW1)

    assert st.reset_extract_in_progress(conn) == 1

    states = {
        r["content_sha256"]: r["extract_state"]
        for r in conn.execute(
            "SELECT content_sha256, extract_state FROM raw_docs"
        ).fetchall()
    }
    assert states == {"h1": "pending", "h2": "done", "h3": "rejected", "h4": "error"}


def test_reset_extract_errors_requeues_only_errors():
    conn = mem()
    assert st.upsert_raw_doc(conn, "h1", "html", "/raw/h1.html", 3, NOW1) is True
    assert st.upsert_raw_doc(conn, "h2", "html", "/raw/h2.html", 3, NOW1) is True
    assert st.upsert_raw_doc(conn, "h3", "html", "/raw/h3.html", 3, NOW1) is True
    assert st.upsert_raw_doc(conn, "h4", "html", "/raw/h4.html", 3, NOW1) is True

    claimed = st.claim_pending_raw(conn)
    assert claimed["content_sha256"] == "h1"
    st.save_extraction(conn, "h2", doc(), True, None, None, NOW1)
    st.save_extraction(conn, "h3", doc(), False, "too_short", None, NOW1)
    st.save_extraction(conn, "h4", None, False, None, "boom", NOW1)

    assert st.reset_extract_errors(conn) == 1

    rows = {
        r["content_sha256"]: r
        for r in conn.execute(
            "SELECT content_sha256, extract_state, extract_error FROM raw_docs"
        ).fetchall()
    }
    states = {sha: r["extract_state"] for sha, r in rows.items()}
    assert states == {
        "h1": "in_progress",
        "h2": "done",
        "h3": "rejected",
        "h4": "pending",
    }
    assert rows["h4"]["extract_error"] is None


def test_urls_for_content_only_present():
    conn = mem()
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW1)
    st.mark_url_checked(conn, "https://x/a", 200, None, None, "c1", True, True, NOW1)
    st.enqueue(conn, "https://x/b", "x", 0, None, NOW1)
    st.mark_url_checked(conn, "https://x/b", 200, None, None, "c1", True, True, NOW1)
    st.mark_url_removed(conn, "https://x/b", NOW2)
    rows = st.urls_for_content(conn, "c1")
    assert [r["url"] for r in rows] == ["https://x/a"]


def test_text_hash_collapses_whitespace_and_is_nfc_stable():
    assert st.text_hash("a  b\n\tc ") == st.text_hash("a b c")
    assert st.text_hash("grün") == st.text_hash("grün")
    assert st.text_hash("hello") != st.text_hash("world")


def test_canonical_key_prefers_fewest_params_then_shortest():
    base = "https://x/dualis-firmenliste/"
    variant = "https://x/dualis-firmenliste/?tx=1&cHash=abc"
    assert st._canonical_key(base) < st._canonical_key(variant)


def test_upsert_document_populates_text_sha256():
    conn = mem()
    st.upsert_document(
        conn, "https://x/a", "x", "html", "c1", doc("some text " * 20), NOW1
    )
    row = conn.execute(
        "SELECT text_sha256 FROM documents WHERE url='https://x/a'"
    ).fetchone()
    assert row["text_sha256"] == st.text_hash("some text " * 20)


def test_upsert_duplicate_variant_creates_no_row():
    conn = mem()
    base = "https://x/dualis/"
    variant = "https://x/dualis/?cHash=abc"
    assert st.upsert_document(conn, base, "x", "html", "c1", doc(), NOW1) == "new"
    assert (
        st.upsert_document(conn, variant, "x", "html", "c2", doc(), NOW2) == "duplicate"
    )
    urls = [r["url"] for r in conn.execute("SELECT url FROM documents").fetchall()]
    assert urls == [base]


def test_upsert_cleaner_url_replaces_existing_owner():
    conn = mem()
    base = "https://x/dualis/"
    variant = "https://x/dualis/?cHash=abc"
    assert st.upsert_document(conn, variant, "x", "html", "c2", doc(), NOW1) == "new"
    assert st.upsert_document(conn, base, "x", "html", "c1", doc(), NOW2) == "new"
    urls = [
        r["url"]
        for r in conn.execute("SELECT url FROM documents WHERE present=1").fetchall()
    ]
    assert urls == [base]
    assert (
        conn.execute(
            "SELECT present FROM documents WHERE url=?", (variant,)
        ).fetchone()["present"]
        == 0
    )


def test_retired_duplicate_is_reported_as_a_deletion():
    """A URL already handed downstream, later retired as a dedup duplicate, must surface in delta()'s deletions."""
    conn = mem()
    base = "https://x/dualis/"
    variant = "https://x/dualis/?cHash=abc"

    st.upsert_document(conn, variant, "x", "html", "c2", doc(), NOW1)
    assert {u["url"] for u in st.delta(conn, since=NOW0)["upserts"]} == {variant}

    st.upsert_document(conn, base, "x", "html", "c1", doc(), NOW2)

    d = st.delta(conn, since=NOW1)
    assert base in {u["url"] for u in d["upserts"]}
    assert variant in {x["url"] for x in d["deletions"]}, (
        "the retired duplicate must be reported so downstream drops the orphan"
    )
    assert st.stats(conn)["documents"] == 1


def test_re_retiring_a_duplicate_does_not_re_emit_the_deletion():
    """Retiring is idempotent: updated_at is stamped only on the live -> retired transition, because that stamp is the deletion signal delta() reports."""
    conn = mem()
    base = "https://x/p/"
    variant = "https://x/p/?cHash=abc"
    st.upsert_document(conn, variant, "x", "html", "c2", doc(), NOW1)
    st.upsert_document(conn, base, "x", "html", "c1", doc(), NOW2)

    assert (
        st.upsert_document(conn, variant, "x", "html", "c3", doc(), NOW3) == "duplicate"
    )

    row = conn.execute(
        "SELECT present, updated_at FROM documents WHERE url=?", (variant,)
    ).fetchone()
    assert row["present"] == 0
    assert row["updated_at"] == NOW2, "an already-retired row must not be re-stamped"
    assert variant not in {d["url"] for d in st.delta(conn, since=NOW2)["deletions"]}


def test_retired_duplicate_can_become_canonical_again():
    """Resurrection: a tombstoned URL that later wins again must come back with its content intact and be re-emitted as an upsert."""
    conn = mem()
    base = "https://x/p/"
    variant = "https://x/p/?cHash=abc"
    st.upsert_document(conn, variant, "x", "html", "c2", doc(), NOW1)
    st.upsert_document(conn, base, "x", "html", "c1", doc(), NOW2)

    assert (
        st.upsert_document(
            conn, variant, "x", "html", "c3", doc("fresh text " * 30), NOW3
        )
        == "changed"
    )
    row = conn.execute("SELECT * FROM documents WHERE url=?", (variant,)).fetchone()
    assert row["present"] == 1
    assert "fresh text" in row["text"]
    assert variant in {u["url"] for u in st.delta(conn, since=NOW2)["upserts"]}


def test_upsert_bytes_change_same_text_does_not_bump_revision():
    conn = mem()
    st.upsert_document(conn, "https://x/a", "x", "html", "c1", doc(), NOW1)
    assert (
        st.upsert_document(conn, "https://x/a", "x", "html", "c2", doc(), NOW2)
        == "unchanged"
    )
    row = conn.execute("SELECT * FROM documents WHERE url='https://x/a'").fetchone()
    assert row["revision"] == 1
    assert row["updated_at"] == NOW1
    assert row["content_sha256"] == "c2"


_OLD_DOCUMENTS_DDL = """
CREATE TABLE documents (
    id               TEXT PRIMARY KEY,
    url              TEXT NOT NULL UNIQUE,
    final_url        TEXT,
    site             TEXT NOT NULL,
    source_type      TEXT NOT NULL,
    content_sha256   TEXT NOT NULL,
    title            TEXT,
    text             TEXT NOT NULL,
    markdown         TEXT NOT NULL,
    lang             TEXT,
    word_count       INTEGER NOT NULL,
    metadata         TEXT,
    present          INTEGER NOT NULL DEFAULT 1,
    revision         INTEGER NOT NULL DEFAULT 1,
    first_indexed_at TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
"""


def test_migration_adds_text_sha256_to_preexisting_db():
    conn = st.connect(":memory:")
    conn.executescript(_OLD_DOCUMENTS_DDL)
    conn.execute(
        "INSERT INTO documents (id, url, final_url, site, source_type, content_sha256,"
        " title, text, markdown, lang, word_count, metadata, present, revision,"
        " first_indexed_at, updated_at) VALUES"
        " ('id1','https://x/a','https://x/a','x','html','c1','T','body','body',"
        " 'en',1,NULL,1,1,?,?)",
        (NOW1, NOW1),
    )
    conn.commit()

    st.init_db(conn)

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
    assert "text_sha256" in cols
    row = conn.execute(
        "SELECT text_sha256 FROM documents WHERE url='https://x/a'"
    ).fetchone()
    assert row["text_sha256"] is None


def test_migration_is_idempotent_when_column_present():
    conn = mem()
    st.init_db(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
    assert "text_sha256" in cols


def test_upsert_fills_missing_title_from_url_basename():
    conn = mem()
    st.enqueue(conn, "https://x/fileadmin/Modulhandbuch_WI.pdf", "x", 0, None, NOW1)
    titleless = {
        "title": None,
        "text": "genuine module handbook body text " * 10,
        "markdown": "genuine module handbook body text " * 10,
        "lang": None,
        "word_count": 60,
        "metadata": None,
    }
    st.upsert_document(
        conn,
        "https://x/fileadmin/Modulhandbuch_WI.pdf",
        "x",
        "pdf",
        "c1",
        titleless,
        NOW1,
    )
    row = conn.execute(
        "SELECT title FROM documents WHERE url='https://x/fileadmin/Modulhandbuch_WI.pdf'"
    ).fetchone()
    assert row["title"] == "Modulhandbuch WI"


def test_upsert_keeps_existing_title():
    conn = mem()
    st.enqueue(conn, "https://x/a", "x", 0, None, NOW1)
    withtitle = {
        "title": "Real Title",
        "text": "some body text here " * 10,
        "markdown": "some body text here " * 10,
        "lang": None,
        "word_count": 40,
        "metadata": None,
    }
    st.upsert_document(conn, "https://x/a", "x", "html", "c1", withtitle, NOW1)
    row = conn.execute("SELECT title FROM documents WHERE url='https://x/a'").fetchone()
    assert row["title"] == "Real Title"
