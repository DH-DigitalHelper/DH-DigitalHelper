import json

from scraper import chunk, storage


def test_chunk_markdown_respects_structure_and_limit():
    markdown = "# Main\n\n" + " ".join(f"one{i}." for i in range(60))
    markdown += "\n\n## Next\n\n" + " ".join(f"two{i}." for i in range(60))

    chunks = chunk.chunk_markdown(markdown, target_words=50, overlap_words=10)

    assert len(chunks) >= 3
    # The configured limit applies to the body; repeated heading breadcrumbs add
    # at most the heading depth on top.
    assert all(len(text.split()) <= 54 for text, _, _ in chunks)
    assert any("Next" in json.loads(headings)[0] for _, _, headings in chunks[-2:])


def test_chunk_markdown_applies_word_overlap_within_section():
    markdown = "# Main\n\n" + " ".join(f"w{i}" for i in range(130))

    chunks = chunk.chunk_markdown(markdown, target_words=50, overlap_words=10)
    first = chunks[0][0].split()[1:]
    second = chunks[1][0].split()[1:]

    assert first[-10:] == second[:10]
    assert all(len(text.split()) <= 50 for text, _, _ in chunks)


def test_run_chunking_preserves_document_metadata_and_is_idempotent(tmp_path):
    conn = storage.connect(tmp_path / "db.sqlite3")
    storage.init_db(conn)
    conn.execute(
        """INSERT INTO documents (
               id,url,site,source_type,content_sha256,title,text,markdown,lang,
               word_count,metadata,text_sha256,present,revision,first_indexed_at,
               updated_at,standort_id,department_id,classify_meta
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "doc-1",
            "https://x/a",
            "x",
            "html",
            "raw",
            "Title",
            "alpha beta gamma",
            "# Intro\n\nalpha beta gamma",
            "de",
            3,
            '{"author":"A"}',
            storage.text_hash("alpha beta gamma"),
            1,
            2,
            "2026-01-01",
            "2026-01-01",
            1,
            2,
            '{"version":2}',
        ),
    )
    conn.commit()

    first = chunk.run_chunking(conn, target_words=50, overlap_words=5)
    second = chunk.run_chunking(conn, target_words=50, overlap_words=5)
    row = conn.execute("SELECT * FROM document_chunks").fetchone()

    assert first["documents"] == 1 and first["chunks"] == 1
    assert second["unchanged"] == 1 and second["chunks"] == 0
    assert row["document_id"] == "doc-1"
    assert row["url"] == "https://x/a"
    assert row["metadata"] == '{"author":"A"}'
    assert row["standort_id"] == 1 and row["department_id"] == 2
