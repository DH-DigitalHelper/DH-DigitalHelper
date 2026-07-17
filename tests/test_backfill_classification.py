import importlib.util
from pathlib import Path

from scraper import storage as st

NOW = "2026-07-17T00:00:00"
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_classification.py"


def _load():
    spec = importlib.util.spec_from_file_location("backfill_classification", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def doc(text="hello world " * 20):
    return {
        "title": "T",
        "text": text,
        "markdown": text,
        "lang": "de",
        "word_count": len(text.split()),
        "metadata": {"description": ""},
    }


def _seed_unclassified(conn, url, site):
    st.enqueue(conn, url, site, 0, None, NOW)
    st.mark_url_checked(conn, url, 200, None, None, "c1", True, True, NOW)
    st.upsert_document(conn, url, site, "html", "c1", doc(), NOW)
    # simulate a pre-feature row: clear the enrichment columns
    conn.execute(
        "UPDATE documents SET standort_id=NULL, department_id=NULL, "
        "study_program_id=NULL, classify_meta=NULL WHERE url=?",
        (url,),
    )
    conn.commit()


def test_backfill_populates_ids_without_touching_updated_at(tmp_path):
    mod = _load()
    conn = st.connect(str(tmp_path / "db.sqlite3"))
    st.init_db(conn)
    url = "https://www.dhbw-stuttgart.de/fakultaet-wirtschaft/"
    _seed_unclassified(conn, url, "dhbw-stuttgart.de")
    before = conn.execute(
        "SELECT updated_at FROM documents WHERE url=?", (url,)
    ).fetchone()["updated_at"]

    result = mod.backfill_classification(conn, batch_size=10)

    assert result["updated"] == 1
    row = conn.execute("SELECT * FROM documents WHERE url=?", (url,)).fetchone()
    dept = conn.execute(
        "SELECT name FROM departments WHERE id=?", (row["department_id"],)
    ).fetchone()
    assert dept["name"] == "wirtschaft"
    assert row["standort_id"] is not None
    assert row["updated_at"] == before  # untouched


def test_backfill_is_idempotent(tmp_path):
    mod = _load()
    conn = st.connect(str(tmp_path / "db.sqlite3"))
    st.init_db(conn)
    _seed_unclassified(conn, "https://www.mannheim.dhbw.de/x", "mannheim.dhbw.de")
    mod.backfill_classification(conn, batch_size=10)
    first = conn.execute(
        "SELECT standort_id, department_id, classify_meta FROM documents"
    ).fetchone()
    mod.backfill_classification(conn, batch_size=10)
    second = conn.execute(
        "SELECT standort_id, department_id, classify_meta FROM documents"
    ).fetchone()
    assert tuple(first) == tuple(second)
