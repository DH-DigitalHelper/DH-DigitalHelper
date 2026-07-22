from pathlib import Path

import chromadb
from chromadb.api import ClientAPI

from .embedding import (
    EMBEDDING_VERSION,
    MODEL_DIMENSIONS,
    Embedder,
    embed_chroma_batch,
    iter_chunk_row_batches,
    metadata_for_row,
    runtime_model_name,
)


class ChromaError(RuntimeError):
    """The configured Chroma store cannot be accessed or synchronized safely."""


def create_client(
    mode: str = "server",
    host: str = "localhost",
    port: int = 8000,
    path: str = "./chroma_data",
) -> ClientAPI:
    if mode == "server":
        return chromadb.HttpClient(host=host, port=port)
    elif mode == "persistent":
        return chromadb.PersistentClient(path=path)
    elif mode == "memory":
        return chromadb.Client()
    else:
        raise ValueError(f"Unbekannter mode: {mode}")


def get_collection(client: ClientAPI, name: str = "dhbw_heidenheim"):
    return client.get_or_create_collection(
        name=name, configuration={"hnsw": {"space": "cosine"}}
    )


def index_chunks(
    collection,
    input_path: Path,
    *,
    model_name: str,
    device: str,
    batch_size: int,
    cache_dir: Path,
    limit: int | None = None,
    embedder: Embedder | None = None,
    prune_stale: bool = True,
    delete_batch_size: int = 5_000,
) -> dict[str, int]:
    """Synchronize one dedicated Chroma collection with the SQLite chunk snapshot.

    A bounded ``limit`` is a preview/benchmark operation and therefore never prunes
    records that were outside that partial source view.
    """
    if delete_batch_size < 1:
        raise ValueError("delete_batch_size must be >= 1")
    if device not in ("cpu", "cuda"):
        raise ValueError("device must be 'cpu' or 'cuda'")

    try:
        target = collection.get(include=["metadatas"])
        target_metadata = dict(zip(target["ids"], target["metadatas"]))
    except Exception as exc:
        raise ChromaError(f"Could not enumerate Chroma records: {exc}") from exc

    runtime_name = runtime_model_name(model_name)
    expected_dimension = MODEL_DIMENSIONS.get(model_name)
    runtime_embedder = embedder
    actual_dimension: int | None = None
    source_ids: set[str] = set()
    upserted = 0
    metadata_updated = 0
    unchanged = 0

    for rows in iter_chunk_row_batches(input_path, batch_size=batch_size, limit=limit):
        embed_rows = []
        update_rows = []
        for row in rows:
            chunk_id = row["chunk_id"]
            source_ids.add(chunk_id)
            current = target_metadata.get(chunk_id)
            dimension = current.get("embedding_dimension") if current else None
            embedding_current = (
                current is not None
                and current.get("content_sha256") == row["content_sha256"]
                and current.get("embedding_model") == model_name
                and current.get("embedding_runtime_model") == runtime_name
                and current.get("embedding_version") == EMBEDDING_VERSION
                and isinstance(dimension, int)
                and dimension > 0
                and (expected_dimension is None or dimension == expected_dimension)
            )
            if not embedding_current:
                embed_rows.append(row)
                continue
            if actual_dimension is None:
                actual_dimension = dimension
            elif actual_dimension != dimension:
                raise ChromaError(
                    "Existing Chroma metadata contains inconsistent embedding "
                    f"dimensions: {actual_dimension} and {dimension}"
                )
            desired = metadata_for_row(
                row,
                model_name=model_name,
                runtime_model_name=runtime_name,
                dimension=dimension,
            )
            if current != desired:
                update_rows.append((row, dimension))
            else:
                unchanged += 1

        if embed_rows:
            batch, runtime_embedder, actual_dimension = embed_chroma_batch(
                embed_rows,
                model_name=model_name,
                device=device,
                batch_size=batch_size,
                cache_dir=cache_dir,
                embedder=runtime_embedder,
                previous_dimension=actual_dimension,
            )
            try:
                collection.upsert(**batch)
            except Exception as exc:
                raise ChromaError(f"Chroma upsert failed: {exc}") from exc
            upserted += len(batch["ids"])

        if update_rows:
            ids = [row["chunk_id"] for row, _ in update_rows]
            metadatas = [
                metadata_for_row(
                    row,
                    model_name=model_name,
                    runtime_model_name=runtime_name,
                    dimension=dimension,
                )
                for row, dimension in update_rows
            ]
            try:
                collection.update(ids=ids, metadatas=metadatas)
            except Exception as exc:
                raise ChromaError(f"Chroma metadata update failed: {exc}") from exc
            metadata_updated += len(ids)

    deleted = 0
    if prune_stale and limit is None:
        target_ids = set(target_metadata)
        stale_ids = sorted(target_ids - source_ids)
        for offset in range(0, len(stale_ids), delete_batch_size):
            ids = stale_ids[offset : offset + delete_batch_size]
            try:
                collection.delete(ids=ids)
            except Exception as exc:
                raise ChromaError(f"Could not delete stale Chroma IDs: {exc}") from exc
            deleted += len(ids)

    return {
        "upserted": upserted,
        "metadata_updated": metadata_updated,
        "unchanged": unchanged,
        "deleted": deleted,
    }


def search(
    collection,
    query_embedding: list[float],
    top_k: int = 5,
    source_filter: str | None = None,
) -> list[dict]:
    where = {"source_url": source_filter} if source_filter else None
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k,
        where=where,
    )

    return [
        {"text": doc, "metadata": meta, "distance": dist}
        for doc, meta, dist in zip(
            results["documents"][0], results["metadatas"][0], results["distances"][0]
        )
    ]
