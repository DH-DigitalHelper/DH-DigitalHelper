"""Tests for the one-time metadata backfill (storage.run_backfill).

Seeds `documents` rows with the three fields dead exactly as the real corpus has
them (lang NULL, final_url == url, title NULL) plus the crawl_log rows the redirect
truth is recovered from, then checks each field is repaired without bumping
updated_at, and that a second run is a no-op.
"""

from scraper import storage as st

NOW1 = "2026-07-14T00:00:00"
RUN = "run-1"


def db(tmp_path):
    conn = st.connect(str(tmp_path / "backfill.sqlite3"))
    st.init_db(conn)
    return conn


def insert_doc(
    conn, url, text, sha, source_type="html", title=None, present=1, now=NOW1
):
    """A document row with lang NULL, final_url == url, title as given (default NULL)."""
    conn.execute(
        """INSERT INTO documents (id, url, final_url, site, source_type,
               content_sha256, title, text, markdown, lang, word_count, metadata,
               text_sha256, present, revision, first_indexed_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,NULL,?,NULL,?,?,1,?,?)""",
        (
            st._doc_id(url),
            url,
            url,
            "x",
            source_type,
            sha,
            title,
            text,
            text,
            len(text.split()),
            st.text_hash(text),
            present,
            now,
            now,
        ),
    )
    conn.commit()


def log_fetch(conn, url, final_url, sha, now=NOW1, status=200):
    st.record_fetch(
        conn,
        RUN,
        url,
        final_url,
        "x",
        status,
        "text/html",
        sha,
        100,
        "html",
        "changed",
        None,
        now,
    )


def test_backfill_populates_lang(tmp_path):
    conn = db(tmp_path)
    insert_doc(
        conn,
        "https://x/a",
        "Die Studierenden absolvieren Praxisphasen bei ihrem dualen Partner im "
        "Unternehmen und lernen die Praxis kennen.",
        "c1",
    )
    result = st.run_backfill(conn, tmp_path / "raw")
    assert result["lang"] == 1
    row = conn.execute("SELECT lang FROM documents WHERE url='https://x/a'").fetchone()
    assert row["lang"] == "de"


def test_backfill_sets_final_url_from_matching_crawl_log(tmp_path):
    conn = db(tmp_path)
    insert_doc(conn, "https://x/login-page", "some body text here " * 10, "c1")
    log_fetch(conn, "https://x/login-page", "https://x/auth/signin", "c1")

    result = st.run_backfill(conn, tmp_path / "raw")

    assert result["final_url"] == 1
    row = conn.execute(
        "SELECT final_url FROM documents WHERE url='https://x/login-page'"
    ).fetchone()
    assert row["final_url"] == "https://x/auth/signin"


def test_backfill_final_url_ignores_later_304_and_matches_bytes(tmp_path):
    """A later 304 recheck logs final_url == url; the sha match must keep the real
    redirect from the full fetch that produced this doc's bytes."""
    conn = db(tmp_path)
    insert_doc(conn, "https://x/p", "body text of the page " * 10, "c1")
    log_fetch(
        conn, "https://x/p", "https://x/real-target", "c1", now="2026-07-14T00:00:00"
    )
    # a later 304 row: build_batch writes final_url == url and sha == the cached sha
    st.record_fetch(
        conn,
        "run-2",
        "https://x/p",
        "https://x/p",
        "x",
        304,
        None,
        "c1",
        0,
        None,
        "unchanged",
        None,
        "2026-07-15T00:00:00",
    )
    st.run_backfill(conn, tmp_path / "raw")
    row = conn.execute(
        "SELECT final_url FROM documents WHERE url='https://x/p'"
    ).fetchone()
    assert row["final_url"] == "https://x/real-target"


def test_backfill_leaves_final_url_when_no_redirect(tmp_path):
    conn = db(tmp_path)
    insert_doc(conn, "https://x/plain", "ordinary page body text " * 10, "c1")
    log_fetch(conn, "https://x/plain", "https://x/plain", "c1")  # no redirect
    result = st.run_backfill(conn, tmp_path / "raw")
    assert result["final_url"] == 0
    row = conn.execute(
        "SELECT final_url FROM documents WHERE url='https://x/plain'"
    ).fetchone()
    assert row["final_url"] == "https://x/plain"


def test_backfill_titles_missing_html_from_url(tmp_path):
    conn = db(tmp_path)
    insert_doc(
        conn, "https://x/pruefungs-ordnung", "body " * 20, "c1", source_type="html"
    )
    result = st.run_backfill(conn, tmp_path / "raw")
    assert result["titles"] == 1
    row = conn.execute(
        "SELECT title FROM documents WHERE url='https://x/pruefungs-ordnung'"
    ).fetchone()
    assert row["title"] == "pruefungs ordnung"


def test_backfill_pdf_title_prefers_cached_metadata(tmp_path, monkeypatch):
    conn = db(tmp_path)
    insert_doc(
        conn, "https://x/doc.pdf", "pdf body text here " * 10, "cpdf", source_type="pdf"
    )
    # a cached blob must exist at the content-addressed path for metadata to be read
    cache = st.RawCache(tmp_path / "raw")
    path = cache.path_for("cpdf", ".pdf")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF fake bytes")
    from scraper import pdf_extract

    monkeypatch.setattr(
        pdf_extract, "_meta_title", lambda data: "Embedded Ordnung Title"
    )

    st.run_backfill(conn, tmp_path / "raw")

    row = conn.execute(
        "SELECT title FROM documents WHERE url='https://x/doc.pdf'"
    ).fetchone()
    assert row["title"] == "Embedded Ordnung Title"


def test_backfill_pdf_title_falls_back_to_url_when_blob_missing(tmp_path):
    conn = db(tmp_path)
    # no cached blob on disk -> from_url fallback
    insert_doc(
        conn,
        "https://x/fileadmin/Modul_Handbuch.pdf",
        "pdf body " * 20,
        "cx",
        source_type="pdf",
    )
    st.run_backfill(conn, tmp_path / "raw")
    row = conn.execute(
        "SELECT title FROM documents WHERE url='https://x/fileadmin/Modul_Handbuch.pdf'"
    ).fetchone()
    assert row["title"] == "Modul Handbuch"


def test_backfill_does_not_bump_updated_at(tmp_path):
    conn = db(tmp_path)
    insert_doc(
        conn, "https://x/a", "German body ist hier drin genug Text vorhanden " * 5, "c1"
    )
    log_fetch(conn, "https://x/a", "https://x/redirected", "c1")
    st.run_backfill(conn, tmp_path / "raw")
    row = conn.execute(
        "SELECT updated_at FROM documents WHERE url='https://x/a'"
    ).fetchone()
    assert row["updated_at"] == NOW1


def test_backfill_is_idempotent(tmp_path):
    conn = db(tmp_path)
    insert_doc(
        conn,
        "https://x/a",
        "genug deutscher Fliesstext fuer die Erkennung hier " * 4,
        "c1",
    )
    log_fetch(conn, "https://x/a", "https://x/z", "c1")
    first = st.run_backfill(conn, tmp_path / "raw")
    assert first["lang"] == 1 and first["final_url"] == 1 and first["titles"] == 1
    second = st.run_backfill(conn, tmp_path / "raw")
    assert second["lang"] == 0 and second["final_url"] == 0 and second["titles"] == 0


def test_backfill_skips_removed_rows(tmp_path):
    conn = db(tmp_path)
    insert_doc(conn, "https://x/gone", "removed body text here " * 10, "c1", present=0)
    result = st.run_backfill(conn, tmp_path / "raw")
    assert result["scanned"] == 0
