from pathlib import Path
import chromadb
from chromadb.api import ClientAPI
from .embedding import Embedder, iter_chroma_batches


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
) -> int:
    total = 0

    for batch in iter_chroma_batches(
        input_path,
        model_name=model_name,
        device=device,
        batch_size=batch_size,
        cache_dir=cache_dir,
        limit=limit,
        embedder=embedder,
    ):
        collection.upsert(**batch)
        total += len(batch["ids"])

    return total


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
