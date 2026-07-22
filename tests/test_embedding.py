import math
import sys
from types import SimpleNamespace

import pytest

from scraper import (
    chromaDB,
    chunk,
    embedding,
    storage,
)  # Chroma DB ist auch noch neu :)


class FakeEmbedder:
    package_version = "test"

    def __init__(self, *, invalid=False, missing=False, changing=False):
        self.calls = []
        self.invalid = invalid
        self.missing = missing
        self.changing = changing
        self.embedded = 0

    def embed(self, texts, *, batch_size):
        self.calls.append((list(texts), batch_size))
        vectors = []
        for text in texts:
            dimension = 767 if self.changing and self.embedded else 768
            first = math.nan if self.invalid else float(len(text))
            vectors.append([first, *([0.0] * (dimension - 1))])
            self.embedded += 1
        return vectors[:-1] if self.missing else vectors


def _source_db(path, count=3):
    conn = storage.connect(path)
    storage.init_db(conn)
    standort_id = conn.execute(
        "SELECT id FROM standorte WHERE name = 'heidenheim'"
    ).fetchone()[0]
    department_id = conn.execute(
        "SELECT id FROM departments ORDER BY id LIMIT 1"
    ).fetchone()[0]
    conn.execute(
        """INSERT INTO study_programs (name, display_name, department_id)
           VALUES ('test-program', 'Test Program', ?)""",
        (department_id,),
    )
    program_id = conn.execute(
        "SELECT id FROM study_programs WHERE name = 'test-program'"
    ).fetchone()[0]
    for index in range(count):
        text = f"Information number {index} for the dual study program."
        conn.execute(
            """INSERT INTO documents (
                   id,url,final_url,site,source_type,content_sha256,title,text,
                   markdown,lang,word_count,metadata,present,revision,
                   first_indexed_at,updated_at,text_sha256,standort_id,
                   department_id,study_program_id,classify_meta
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"doc-{index}",
                f"https://example.test/{index}",
                f"https://canonical.test/{index}",
                "heidenheim",
                "html",
                f"raw-{index}",
                f"Title {index}",
                text,
                f"# Section {index}\n\n{text}",
                "de",
                len(text.split()),
                "{}",
                1,
                1,
                "2026-01-01",
                "2026-01-01",
                storage.text_hash(text),
                standort_id,
                department_id,
                program_id,
                "{}",
            ),
        )
    conn.commit()
    chunk.run_chunking(conn, target_words=50, overlap_words=5)
    conn.close()


def test_chroma_batches_are_limited_and_preserve_source_metadata(tmp_path):
    source = tmp_path / "source.sqlite3"
    _source_db(source, count=3)
    fake = FakeEmbedder()

    batches = list(
        embedding.iter_chroma_batches(source, batch_size=2, limit=3, embedder=fake)
    )

    assert [len(batch["ids"]) for batch in batches] == [2, 1]
    assert all(
        set(batch) == {"ids", "embeddings", "documents", "metadatas"}
        for batch in batches
    )
    assert all(
        len(batch["ids"])
        == len(batch["embeddings"])
        == len(batch["documents"])
        == len(batch["metadatas"])
        for batch in batches
    )
    assert all(
        len(vector) == 768 for batch in batches for vector in batch["embeddings"]
    )
    first = batches[0]
    assert first["ids"][0] == first["metadatas"][0]["chunk_id"]
    assert "Information number" in first["documents"][0]
    assert first["metadatas"][0]["source_url"].startswith("https://canonical.test/")
    assert first["metadatas"][0]["standort_name"] == "heidenheim"
    assert first["metadatas"][0]["study_program_name"] == "test-program"
    assert first["metadatas"][0]["embedding_dimension"] == 768
    assert None not in first["metadatas"][0].values()
    assert fake.calls[0][1] == 2
    assert list(tmp_path.glob("*.sqlite3")) == [source]


def test_chroma_batch_limit_does_not_fill_a_complete_batch(tmp_path):
    source = tmp_path / "source.sqlite3"
    _source_db(source, count=3)

    batches = list(
        embedding.iter_chroma_batches(
            source, batch_size=3, limit=2, embedder=FakeEmbedder()
        )
    )

    assert len(batches) == 1
    assert len(batches[0]["ids"]) == 2


@pytest.mark.parametrize(
    ("fake", "message"),
    [
        (FakeEmbedder(invalid=True), "finite"),
        (FakeEmbedder(missing=True), "1 vectors for 2 chunks"),
        (FakeEmbedder(changing=True), "must produce 768 dimensions"),
    ],
)
def test_invalid_embedding_batches_are_rejected(tmp_path, fake, message):
    source = tmp_path / "source.sqlite3"
    _source_db(source, count=2)

    with pytest.raises(embedding.EmbeddingError, match=message):
        list(embedding.iter_chroma_batches(source, batch_size=2, embedder=fake))


def test_custom_model_dimension_must_remain_constant(tmp_path):
    source = tmp_path / "source.sqlite3"
    _source_db(source, count=2)

    with pytest.raises(embedding.EmbeddingError, match="dimension changed"):
        list(
            embedding.iter_chroma_batches(
                source,
                model_name="custom/model",
                batch_size=2,
                embedder=FakeEmbedder(changing=True),
            )
        )


def test_embedding_smoke_returns_summary_without_vectors(tmp_path):
    source = tmp_path / "source.sqlite3"
    _source_db(source, count=3)

    result = embedding.run_embedding_smoke(
        source, batch_size=2, limit=2, embedder=FakeEmbedder()
    )

    assert result == {
        "status": "ok",
        "tested_chunks": 2,
        "model": embedding.DEFAULT_MODEL,
        "runtime_model": embedding.DEFAULT_RUNTIME_MODEL,
        "device": "cpu",
        "dimension": 768,
        "batch_size": 2,
        "batches": 1,
        "fastembed_version": "test",
        "preview": {
            "chunk_id": result["preview"]["chunk_id"],
            "text": result["preview"]["text"],
            "embedding_first_10": [
                float(len(result["preview"]["text"])),
                *([0.0] * 9),
            ],
        },
    }
    assert "Information number" in result["preview"]["text"]
    assert len(result["preview"]["embedding_first_10"]) == 10
    assert "embeddings" not in result


def test_embedding_smoke_rejects_database_without_loading_model(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite3"
    conn = storage.connect(source)
    storage.init_db(conn)
    conn.close()
    monkeypatch.setattr(
        embedding,
        "FastEmbedder",
        lambda *args, **kwargs: pytest.fail("model must not be loaded"),
    )

    with pytest.raises(embedding.EmbeddingError, match="No document chunks"):
        embedding.run_embedding_smoke(source)


def test_cuda_provider_fails_closed(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        SimpleNamespace(get_available_providers=lambda: ["CPUExecutionProvider"]),
    )

    with pytest.raises(embedding.EmbeddingError, match="CUDAExecutionProvider"):
        embedding._providers_for_device("cuda")


def test_cuda_provider_is_selected_when_available(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        SimpleNamespace(
            get_available_providers=lambda: [
                "CUDAExecutionProvider",
                "CPUExecutionProvider",
            ]
        ),
    )

    assert embedding._providers_for_device("cuda") == ["CUDAExecutionProvider"]


############################################################################


def test_embedding_batches_can_be_stored_and_queried(tmp_path):
    source = tmp_path / "source.sqlite3"
    _source_db(source, count=3)

    fake = FakeEmbedder()
    embedding.iter_chroma_batches(
        source,
        batch_size=2,
        limit=3,
        embedder=fake,
    )

    client = chromaDB.create_client(
        mode="persistent", path=str(tmp_path / "chroma_data")
    )
    collection = chromaDB.get_collection(
        client,
        name="test_collection",
    )

    stored = 0
    stored = chromaDB.index_chunks(
        collection,
        source,
        model_name=embedding.DEFAULT_MODEL,
        device="cpu",
        batch_size=2,
        cache_dir=tmp_path / "models",
        limit=3,
        embedder=fake,
    )

    assert stored == 3
    assert collection.count() == 3

    result = chromaDB.search(
        collection,
        query_embedding=[0.0] * 768,
        top_k=1,
    )

    assert len(result) == 1
    assert "text" in result[0]
