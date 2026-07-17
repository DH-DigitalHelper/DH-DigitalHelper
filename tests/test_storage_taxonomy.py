# tests/test_storage_taxonomy.py
from scraper import storage as st

_OLD_DOCUMENTS_DDL = """
CREATE TABLE documents (
    id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, final_url TEXT, site TEXT NOT NULL,
    source_type TEXT NOT NULL, content_sha256 TEXT NOT NULL, title TEXT,
    text TEXT NOT NULL, markdown TEXT NOT NULL, lang TEXT, word_count INTEGER NOT NULL,
    metadata TEXT, present INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1,
    first_indexed_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""


def mem():
    conn = st.connect(":memory:")
    st.init_db(conn)
    return conn


def test_init_db_seeds_the_five_departments():
    conn = mem()
    names = {r["name"] for r in conn.execute("SELECT name FROM departments")}
    assert names == {"technik", "wirtschaft", "sozialwesen", "gesundheit", "unknown"}


def test_init_db_seeds_standorte_with_satellite_parents():
    conn = mem()
    rows = {r["name"]: r for r in conn.execute("SELECT * FROM standorte")}
    assert rows["stuttgart"]["kind"] == "campus"
    horb = rows["stuttgart-horb"]
    assert horb["kind"] == "satellite"
    assert horb["parent_id"] == rows["stuttgart"]["id"]


def test_documents_gains_classification_columns():
    conn = mem()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
    assert {"standort_id", "department_id", "study_program_id", "classify_meta"} <= cols


def test_seeding_is_idempotent():
    conn = mem()
    st.init_db(conn)  # second run
    assert conn.execute("SELECT COUNT(*) c FROM departments").fetchone()["c"] == 5
    assert conn.execute("SELECT COUNT(*) c FROM standorte").fetchone()["c"] == 14


def test_migration_adds_columns_to_preexisting_documents_table():
    conn = st.connect(":memory:")
    conn.executescript(_OLD_DOCUMENTS_DDL)
    conn.commit()
    st.init_db(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
    assert {"standort_id", "department_id", "study_program_id", "classify_meta"} <= cols
