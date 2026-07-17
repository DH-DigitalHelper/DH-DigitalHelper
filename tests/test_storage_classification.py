# tests/test_storage_classification.py
import json

from scraper import storage as st

NOW = "2026-07-17T00:00:00"


def mem():
    conn = st.connect(":memory:")
    st.init_db(conn)
    return conn


def doc(text="hello world " * 20, title="T", description=""):
    return {
        "title": title,
        "text": text,
        "markdown": text,
        "lang": "de",
        "word_count": len(text.split()),
        "metadata": {"description": description},
    }


def test_program_id_intern_is_idempotent():
    conn = mem()
    a = st._program_id(conn, "maschinenbau", "Maschinenbau", "technik")
    b = st._program_id(conn, "maschinenbau", "Maschinenbau", "technik")
    assert a == b
    row = conn.execute("SELECT * FROM study_programs WHERE id=?", (a,)).fetchone()
    dept = conn.execute(
        "SELECT name FROM departments WHERE id=?", (row["department_id"],)
    ).fetchone()
    assert dept["name"] == "technik"


def test_upsert_document_sets_standort_and_department_ids():
    conn = mem()
    url = "https://www.dhbw-stuttgart.de/fakultaet-wirtschaft/"
    st.enqueue(conn, url, "dhbw-stuttgart.de", 0, None, NOW)
    st.mark_url_checked(conn, url, 200, None, None, "c1", True, True, NOW)
    st.upsert_document(conn, url, "dhbw-stuttgart.de", "html", "c1", doc(), NOW)
    row = conn.execute("SELECT * FROM documents WHERE url=?", (url,)).fetchone()
    standort = conn.execute(
        "SELECT name FROM standorte WHERE id=?", (row["standort_id"],)
    ).fetchone()
    dept = conn.execute(
        "SELECT name FROM departments WHERE id=?", (row["department_id"],)
    ).fetchone()
    assert standort["name"] == "stuttgart"
    assert dept["name"] == "wirtschaft"
    assert row["study_program_id"] is None
    assert json.loads(row["classify_meta"])["department"] == "url"


def test_upsert_document_faculty_agnostic_page_is_unknown_department():
    conn = mem()
    url = "https://www.heilbronn.dhbw.de/datenschutz/"
    st.enqueue(conn, url, "heilbronn.dhbw.de", 0, None, NOW)
    st.mark_url_checked(conn, url, 200, None, None, "c1", True, True, NOW)
    st.upsert_document(
        conn, url, "heilbronn.dhbw.de", "html", "c1", doc(title="Datenschutz"), NOW
    )
    row = conn.execute("SELECT * FROM documents WHERE url=?", (url,)).fetchone()
    dept = conn.execute(
        "SELECT name FROM departments WHERE id=?", (row["department_id"],)
    ).fetchone()
    assert dept["name"] == "unknown"


def test_upsert_document_detects_program_and_derives_faculty():
    conn = mem()
    url = "https://www.mosbach.dhbw.de/studienangebot/maschinenbau"
    st.enqueue(conn, url, "mosbach.dhbw.de", 0, None, NOW)
    st.mark_url_checked(conn, url, 200, None, None, "c1", True, True, NOW)
    st.upsert_document(conn, url, "mosbach.dhbw.de", "html", "c1", doc(), NOW)
    row = conn.execute("SELECT * FROM documents WHERE url=?", (url,)).fetchone()
    prog = conn.execute(
        "SELECT name, department_id FROM study_programs WHERE id=?",
        (row["study_program_id"],),
    ).fetchone()
    dept = conn.execute(
        "SELECT name FROM departments WHERE id=?", (row["department_id"],)
    ).fetchone()
    assert prog["name"] == "maschinenbau"
    assert dept["name"] == "technik"


def test_stats_reports_department_and_standort_breakdown():
    conn = mem()
    url = "https://www.dhbw-stuttgart.de/fakultaet-wirtschaft/"
    st.enqueue(conn, url, "dhbw-stuttgart.de", 0, None, NOW)
    st.mark_url_checked(conn, url, 200, None, None, "c1", True, True, NOW)
    st.upsert_document(conn, url, "dhbw-stuttgart.de", "html", "c1", doc(), NOW)

    s = st.stats(conn)
    assert s["by_department"].get("wirtschaft") == 1
    assert s["by_standort"].get("stuttgart") == 1
    assert s["unclassified"] == 0
