"""Stream FastEmbed vectors from SQLite chunks in Chroma-compatible batches."""

from __future__ import annotations

import importlib.metadata
import math
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Iterable, Protocol, Sequence, TypeAlias, TypedDict

EMBEDDING_VERSION = 1
DEFAULT_MODEL = "jinaai/jina-embeddings-v2-base-de"
DEFAULT_RUNTIME_MODEL = "dhbw/jina-embeddings-v2-base-de-fp32"
MODEL_DIMENSIONS = {DEFAULT_MODEL: 768}

SOURCE_QUERY = """
SELECT c.id AS chunk_id, c.document_id, c.chunk_index, c.text, c.heading_path,
       COALESCE(NULLIF(c.title, ''), c.url) AS source_title,
       COALESCE(NULLIF(d.final_url, ''), c.url) AS source_url,
       c.site, c.source_type, c.lang, c.standort_id,
       s.name AS standort_name, s.display_name AS standort_display_name,
       c.department_id, dep.name AS department_name,
       dep.display_name AS department_display_name,
       c.study_program_id, sp.name AS study_program_name,
       sp.display_name AS study_program_display_name,
       c.content_sha256, c.document_metadata_sha256, c.document_revision,
       c.metadata AS source_metadata, c.classify_meta
FROM document_chunks c
JOIN documents d ON d.id = c.document_id
LEFT JOIN standorte s ON s.id = c.standort_id
LEFT JOIN departments dep ON dep.id = c.department_id
LEFT JOIN study_programs sp ON sp.id = c.study_program_id
WHERE c.id > ?
ORDER BY c.id
LIMIT ?
"""


class EmbeddingError(RuntimeError):
    """An embedding batch cannot be produced safely."""


class Embedder(Protocol):
    package_version: str

    def embed(
        self, texts: Sequence[str], *, batch_size: int
    ) -> Iterable[Sequence[float]]: ...


class FastEmbedder:
    """Small adapter that keeps FastEmbed optional until it is needed."""

    def __init__(self, model_name: str, *, device: str, cache_dir: Path):
        providers = _providers_for_device(device)
        try:
            from fastembed import TextEmbedding
            from fastembed.common.model_description import ModelSource, PoolingType
        except ImportError as exc:
            extra = "embedding-gpu" if device == "cuda" else "embedding-cpu"
            raise EmbeddingError(
                f"FastEmbed is not installed; run `uv sync --extra {extra}` first."
            ) from exc

        cache_dir.mkdir(parents=True, exist_ok=True)
        runtime_name = runtime_model_name(model_name)
        if runtime_name != model_name:
            # The bundled FP16 graph fails on Windows, so use the upstream FP32 graph.
            if not any(
                item["model"] == runtime_name
                for item in TextEmbedding.list_supported_models()
            ):
                TextEmbedding.add_custom_model(
                    model=runtime_name,
                    pooling=PoolingType.MEAN,
                    normalization=True,
                    sources=ModelSource(hf=DEFAULT_MODEL),
                    dim=MODEL_DIMENSIONS[DEFAULT_MODEL],
                    model_file="onnx/model.onnx",
                    description="FP32 runtime variant of Jina German/English v2",
                    license="apache-2.0",
                    size_in_gb=0.64,
                )
        self._model = TextEmbedding(
            model_name=runtime_name,
            cache_dir=str(cache_dir),
            providers=providers,
        )
        self.package_version = _fastembed_version(device)
        if device == "cuda":
            actual = _model_providers(self._model)
            if "CUDAExecutionProvider" not in actual:
                raise EmbeddingError(
                    "CUDA was requested, but the model session is not using "
                    f"CUDAExecutionProvider (active providers: {actual})."
                )

    def embed(
        self, texts: Sequence[str], *, batch_size: int
    ) -> Iterable[Sequence[float]]:
        return self._model.embed(list(texts), batch_size=batch_size)


MetadataValue: TypeAlias = str | int | float | bool


class ChromaBatch(TypedDict):
    """Keyword arguments accepted by ``chromadb.Collection.upsert``."""

    ids: list[str]
    embeddings: list[list[float]]
    documents: list[str]
    metadatas: list[dict[str, MetadataValue]]


def runtime_model_name(model_name: str) -> str:
    return DEFAULT_RUNTIME_MODEL if model_name == DEFAULT_MODEL else model_name


def _fastembed_version(device: str) -> str:
    package = "fastembed-gpu" if device == "cuda" else "fastembed"
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _providers_for_device(device: str) -> list[str]:
    if device == "cpu":
        return ["CPUExecutionProvider"]
    if device != "cuda":
        raise ValueError("device must be 'cpu' or 'cuda'")
    try:
        import onnxruntime as ort
    except ImportError as exc:
        raise EmbeddingError(
            "CUDA requires fastembed-gpu/onnxruntime-gpu; run "
            "`uv sync --extra embedding-gpu`."
        ) from exc
    available = ort.get_available_providers()
    if "CUDAExecutionProvider" not in available:
        raise EmbeddingError(
            "CUDAExecutionProvider is unavailable. Check the NVIDIA driver, CUDA, "
            f"CuDNN and fastembed-gpu installation (available: {available})."
        )
    return ["CUDAExecutionProvider"]


def _model_providers(model) -> list[str]:
    """Read FastEmbed's ONNX providers and fail closed if its API changes."""
    current = model
    for _ in range(3):
        if hasattr(current, "get_providers"):
            return list(current.get_providers())
        current = getattr(current, "model", None)
        if current is None:
            break
    raise EmbeddingError("Could not verify the active ONNX execution provider.")


def _connect_source(path: Path) -> sqlite3.Connection:
    path = path.resolve()
    if not path.is_file():
        raise EmbeddingError(f"embedding input not found: {path}")
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _validate_vector(values: Sequence[float]) -> list[float]:
    vector = [float(value) for value in values]
    if not vector or not all(math.isfinite(value) for value in vector):
        raise EmbeddingError("Embedding vectors must be non-empty and finite.")
    return vector


def metadata_for_row(
    row: sqlite3.Row,
    *,
    model_name: str,
    runtime_model_name: str,
    dimension: int,
) -> dict[str, MetadataValue]:
    excluded = {"text"}
    metadata: dict[str, MetadataValue] = {
        key: row[key]
        for key in row.keys()
        if key not in excluded and row[key] is not None
    }
    metadata.update(
        embedding_model=model_name,
        embedding_runtime_model=runtime_model_name,
        embedding_version=EMBEDDING_VERSION,
        embedding_dimension=dimension,
    )
    return metadata


def iter_chunk_row_batches(
    input_path: Path, *, batch_size: int, limit: int | None = None
) -> Iterable[list[sqlite3.Row]]:
    """Stream stable SQLite chunk rows without invoking an embedding model."""
    if batch_size < 1:
        raise ValueError("embedding batch_size must be >= 1")
    if limit is not None and limit < 1:
        raise ValueError("embedding limit must be >= 1")

    remaining = limit
    last_id = ""
    with closing(_connect_source(input_path)) as source:
        try:
            source.execute("BEGIN")
            while remaining is None or remaining > 0:
                fetch_size = batch_size
                if remaining is not None:
                    fetch_size = min(fetch_size, remaining)
                rows = source.execute(SOURCE_QUERY, (last_id, fetch_size)).fetchall()
                if not rows:
                    break
                yield rows
                last_id = rows[-1]["chunk_id"]
                if remaining is not None:
                    remaining -= len(rows)
        except sqlite3.Error as exc:
            raise EmbeddingError(f"could not read embedding chunks: {exc}") from exc


def embed_chroma_batch(
    rows: Sequence[sqlite3.Row],
    *,
    model_name: str,
    device: str,
    batch_size: int,
    cache_dir: Path,
    embedder: Embedder | None = None,
    previous_dimension: int | None = None,
) -> tuple[ChromaBatch, Embedder, int]:
    """Embed selected rows while preserving model and dimension validation."""
    if not rows:
        raise ValueError("cannot embed an empty row batch")
    runtime_embedder = embedder or FastEmbedder(
        model_name, device=device, cache_dir=cache_dir
    )
    try:
        vectors = list(
            runtime_embedder.embed([row["text"] for row in rows], batch_size=batch_size)
        )
    except EmbeddingError:
        raise
    except Exception as exc:
        raise EmbeddingError(str(exc)) from exc
    if len(vectors) != len(rows):
        raise EmbeddingError(
            f"model returned {len(vectors)} vectors for {len(rows)} chunks"
        )

    expected_dimension = MODEL_DIMENSIONS.get(model_name)
    actual_dimension = previous_dimension
    embeddings: list[list[float]] = []
    for raw_vector in vectors:
        vector = _validate_vector(raw_vector)
        dimension = len(vector)
        if expected_dimension is not None and dimension != expected_dimension:
            raise EmbeddingError(
                f"{model_name} must produce {expected_dimension} dimensions; "
                f"got {dimension}"
            )
        if actual_dimension is not None and dimension != actual_dimension:
            raise EmbeddingError(
                f"vector dimension changed from {actual_dimension} to {dimension}"
            )
        actual_dimension = dimension
        embeddings.append(vector)

    assert actual_dimension is not None
    runtime_name = runtime_model_name(model_name)
    batch = ChromaBatch(
        ids=[row["chunk_id"] for row in rows],
        embeddings=embeddings,
        documents=[row["text"] for row in rows],
        metadatas=[
            metadata_for_row(
                row,
                model_name=model_name,
                runtime_model_name=runtime_name,
                dimension=actual_dimension,
            )
            for row in rows
        ],
    )
    return batch, runtime_embedder, actual_dimension


def iter_chroma_batches(
    input_path: Path,
    *,
    model_name: str = DEFAULT_MODEL,
    device: str = "cpu",
    batch_size: int = 8,
    cache_dir: Path = Path("data/models"),
    limit: int | None = None,
    embedder: Embedder | None = None,
) -> Iterable[ChromaBatch]:
    """Yield in-memory batches ready for ``collection.upsert(**batch)``."""
    if batch_size < 1:
        raise ValueError("embedding batch_size must be >= 1")
    if limit is not None and limit < 1:
        raise ValueError("embedding limit must be >= 1")
    if device not in ("cpu", "cuda"):
        raise ValueError("device must be 'cpu' or 'cuda'")

    actual_dimension: int | None = None
    runtime_embedder = embedder
    for rows in iter_chunk_row_batches(input_path, batch_size=batch_size, limit=limit):
        batch, runtime_embedder, actual_dimension = embed_chroma_batch(
            rows,
            model_name=model_name,
            device=device,
            batch_size=batch_size,
            cache_dir=cache_dir,
            embedder=runtime_embedder,
            previous_dimension=actual_dimension,
        )
        yield batch


def run_embedding_smoke(
    input_path: Path,
    *,
    model_name: str = DEFAULT_MODEL,
    device: str = "cpu",
    batch_size: int = 8,
    cache_dir: Path = Path("data/models"),
    limit: int = 5,
    embedder: Embedder | None = None,
) -> dict[str, object]:
    """Embed a bounded real chunk sample without persisting its vectors."""
    if batch_size < 1:
        raise ValueError("embedding batch_size must be >= 1")
    if limit < 1:
        raise ValueError("embedding limit must be >= 1")
    if device not in ("cpu", "cuda"):
        raise ValueError("device must be 'cpu' or 'cuda'")
    with closing(_connect_source(input_path)) as source:
        try:
            has_chunks = source.execute(
                "SELECT 1 FROM document_chunks LIMIT 1"
            ).fetchone()
        except sqlite3.Error as exc:
            raise EmbeddingError(f"could not read embedding chunks: {exc}") from exc
    if has_chunks is None:
        raise EmbeddingError(
            "No document chunks found; run `dhbw-scraper chunk` first."
        )

    runtime_embedder = embedder or FastEmbedder(
        model_name, device=device, cache_dir=cache_dir
    )
    tested = 0
    batches = 0
    dimension: int | None = None
    preview: dict[str, object] | None = None
    for batch in iter_chroma_batches(
        input_path,
        model_name=model_name,
        device=device,
        batch_size=batch_size,
        cache_dir=cache_dir,
        limit=limit,
        embedder=runtime_embedder,
    ):
        batches += 1
        tested += len(batch["ids"])
        dimension = len(batch["embeddings"][0])
        if preview is None:
            preview = {
                "chunk_id": batch["ids"][0],
                "text": batch["documents"][0],
                "embedding_first_10": batch["embeddings"][0][:10],
            }
    assert preview is not None
    return {
        "status": "ok",
        "tested_chunks": tested,
        "model": model_name,
        "runtime_model": runtime_model_name(model_name),
        "device": device,
        "dimension": dimension,
        "batch_size": batch_size,
        "batches": batches,
        "fastembed_version": getattr(runtime_embedder, "package_version", "unknown"),
        "preview": preview,
    }
