# DHBW Scraper and RAG Indexing Pipeline

`dhbw-scraper` crawls the DHBW web presence, extracts HTML and PDF content,
stores the materialized corpus and its chunks in SQLite, and optionally indexes
the resulting embeddings in ChromaDB.

The pipeline consists of four stages:

1. Crawl and download source documents.
2. Extract, classify, and deduplicate the corpus in SQLite.
3. Build structure-aware Markdown chunks in SQLite.
4. Embed changed chunks and synchronize them with ChromaDB.

## Requirements

- Python 3.14
- [uv](https://docs.astral.sh/uv/)
- Rust toolchain
- On Windows: Visual Studio 2022 C++ Build Tools

On Windows, run the initial installation from the **x64 Native Tools Command
Prompt for VS 2022**. The project contains a Rust extension that requires the
MSVC compiler during installation.

## Installation

For development, local ChromaDB, and CPU embeddings:

```powershell
uv sync --extra dev --extra chroma --extra embedding-cpu
```

For CUDA embeddings, use the HTTP-only Chroma client and run the Chroma server
in a separate environment. This prevents the full Chroma package's CPU
`onnxruntime` from colliding with FastEmbed's `onnxruntime-gpu` files:

```powershell
uv sync --extra dev --extra chroma-client --extra embedding-gpu
```

Do not install `chroma` together with `embedding-gpu`, and do not install the CPU
and GPU embedding extras together. These invalid combinations are rejected by
uv.

Run the test suite after installation:

```powershell
uv run pytest
```

## Updating an Existing Checkout

If `data/scraper.sqlite3` already contains a scraped and extracted corpus, the
website does **not** need to be scraped again after pulling this branch.

Run:

```powershell
git pull
uv sync --extra dev --extra chroma --extra embedding-cpu
uv run pytest
uv run dhbw-scraper chunk
uv run dhbw-scraper embedding-smoke --limit 10
```

The `chunk` command performs the one-time migration to chunker version 5 and
rebuilds the derived `document_chunks` table. It does not modify the source
documents or contact any website.

The embedding smoke test calculates ten real embeddings and discards them. A
successful result reports the configured model and an embedding dimension of
768.

### Existing scraped database on a GPU machine

If another developer receives the already scraped `data/scraper.sqlite3`, no
new crawl is required. First check whether the database already contains
materialized documents:

```powershell
uv run dhbw-scraper stats
```

If `documents` is populated, start with chunking. If the database contains only
fetched/raw documents, run `extract` and `dedup` once before `chunk`.

```powershell
uv sync --extra dev --extra chroma-client --extra embedding-gpu
# Only needed when `stats` shows no materialized documents:
# uv run dhbw-scraper extract
# uv run dhbw-scraper dedup
uv run dhbw-scraper chunk
uv run dhbw-scraper embedding-smoke --device cuda --limit 10
```

For GPU mode, change the Chroma section in `config.toml` to HTTP server mode:

```toml
[chroma]
mode = "server"
host = "localhost"
port = 8000
collection = "dhbw_corpus"
```

Start the Chroma server in a separate terminal and isolated uv environment:

```powershell
uvx --from "chromadb==1.5.9" chroma run --path data/chroma --host localhost --port 8000
```

The server environment contains Chroma's CPU runtime; the project `.venv`
contains only `onnxruntime-gpu`. With the server running, start the complete GPU
index from the project terminal:

```powershell
uv run dhbw-scraper index --device cuda
```

The CUDA smoke test fails explicitly unless ONNX Runtime reports an active
`CUDAExecutionProvider`.

## Fresh Checkout Without an Existing Database

The `data/` directory is ignored by Git. A fresh clone therefore contains
neither the scraper database nor a Chroma index.

First review `config.toml`, especially the configured sites, crawl limits, and
`crawl.user_agent`. Then run:

```powershell
uv run dhbw-scraper run
uv run dhbw-scraper chunk
uv run dhbw-scraper embedding-smoke --limit 10
```

`run` executes crawling, extraction, and deduplication. Chunking and vector
indexing are separate commands so that each derived stage can be repeated
without downloading the source documents again.

## Testing Persistent Chroma Storage with 100 Chunks

Before indexing the complete corpus, write a bounded sample to a dedicated test
collection:

```powershell
uv run python -c "from scraper import chromaDB; from scraper.config import load_config; c=load_config(); client=chromaDB.create_client(mode=c.chroma.mode,host=c.chroma.host,port=c.chroma.port,path=str(c.chroma.path)); collection=chromaDB.get_collection(client,'dhbw_smoke_100'); result=chromaDB.index_chunks(collection,c.storage.db_file,model_name=c.embedding.model,device='cpu',batch_size=c.embedding.cpu_batch_size,cache_dir=c.embedding.cache_dir,limit=100); print(result); print('Collection count:',collection.count())"
```

The first run should report approximately:

```text
{'upserted': 100, 'metadata_updated': 0, 'unchanged': 0, 'deleted': 0}
Collection count: 100
```

Run the same command again to verify incremental indexing:

```text
{'upserted': 0, 'metadata_updated': 0, 'unchanged': 100, 'deleted': 0}
Collection count: 100
```

When `limit` is set, records outside the partial source view are never deleted.
Using a separate collection keeps the smoke test isolated from the complete
corpus.

## Inspecting ChromaDB

List all collections and their sizes:

```powershell
uv run python -c "from scraper import chromaDB; from scraper.config import load_config; c=load_config(); client=chromaDB.create_client(mode=c.chroma.mode,host=c.chroma.host,port=c.chroma.port,path=str(c.chroma.path)); print([(collection.name,collection.count()) for collection in client.list_collections()])"
```

Inspect one stored document, its metadata, and embedding dimension:

```powershell
uv run python -c "from scraper import chromaDB; from scraper.config import load_config; c=load_config(); client=chromaDB.create_client(mode=c.chroma.mode,host=c.chroma.host,port=c.chroma.port,path=str(c.chroma.path)); collection=chromaDB.get_collection(client,'dhbw_smoke_100'); r=collection.get(limit=1,include=['documents','metadatas','embeddings']); print('ID:',r['ids'][0]); print('Text:',r['documents'][0]); print('Metadata:',r['metadatas'][0]); print('Embedding dimension:',len(r['embeddings'][0]))"
```

## Indexing the Complete Corpus

After the smoke test succeeds, synchronize the complete corpus:

```powershell
uv run dhbw-scraper index
```

The command first refreshes SQLite chunks and then synchronizes the Chroma
collection configured in `config.toml` (by default `dhbw_corpus`).

The synchronization is incremental:

- New or content-changed chunks are embedded and upserted.
- Metadata-only changes use a Chroma metadata update without recalculating the
  embedding.
- Unchanged chunks are skipped.
- Chroma records no longer present in SQLite are deleted during complete runs.

The first complete CPU run may take a considerable amount of time. Later runs
only calculate embeddings for new or changed content.

## Storage Layout

```text
data/
|-- scraper.sqlite3       Crawling, extracted documents, and chunks
|-- raw/                  Content-addressed raw download cache
|-- models/               Downloaded embedding models
`-- chroma/               Persistent Chroma catalog and HNSW index
    |-- chroma.sqlite3
    `-- <segment-id>/
```

`data/chroma/chroma.sqlite3` is ChromaDB's internal catalog. It is separate from
the scraper's `data/scraper.sqlite3` database and is expected when
`chroma.mode = "persistent"`.

## Main Commands

```text
fetch             Crawl and download source documents
extract           Extract HTML and PDF content
run               Fetch, extract, and deduplicate
chunk             Synchronize structure-aware SQLite chunks
embedding-smoke   Calculate and discard a small embedding sample
index             Refresh chunks and synchronize ChromaDB
stats             Print corpus statistics
report            Generate the local HTML analysis report
```

Run `uv run dhbw-scraper --help` or `uv run dhbw-scraper <command> --help` for
the complete command reference.
