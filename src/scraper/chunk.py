"""Structure-aware, deterministic chunking of the materialized document corpus."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time

from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)

from .storage import normalize_text, text_hash, write_txn

CHUNKER_VERSION = 5
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_WORDS = re.compile(r"\S+")
_LINK = re.compile(r"!?\[([^]]*)\]\([^)]*\)")
_MARKUP = re.compile(r"[`*_~]+")
_HEADING_MARKER = re.compile(r"(?m)^#{1,6}\s+")
_HEADERS = [("#" * level, f"Header {level}") for level in range(1, 7)]
_MARKDOWN_SPLITTER = MarkdownHeaderTextSplitter(
    headers_to_split_on=_HEADERS,
    strip_headers=True,
)


def _word_count(text: str) -> int:
    return len(_WORDS.findall(text))


def _demote_oversized_headings(markdown: str) -> str:
    """Treat PDF slide-sized headings as body text before library splitting."""
    lines = []
    for line in (markdown or "").splitlines():
        match = _HEADING.match(line)
        lines.append(
            match.group(2) if match and _word_count(match.group(2)) > 40 else line
        )
    return "\n".join(lines)


def _recursive_split(text: str, size: int, overlap: int) -> list[str]:
    """Use LangChain's standard recursive splitter with a word-based budget."""
    size = max(1, size)
    overlap = min(overlap, size - 1)
    return RecursiveCharacterTextSplitter(
        chunk_size=size,
        chunk_overlap=overlap,
        length_function=_word_count,
        separators=[r"\n\n", r"(?<=[.!?])\s+", r"\n", r"\s+", ""],
        is_separator_regex=True,
    ).split_text(text)


def _render(
    segments: list[tuple[tuple[str, ...], str]],
) -> tuple[str, str, str]:
    parts = []
    paths = []
    previous_heading = None
    for heading, body in segments:
        if heading != previous_heading:
            parts.extend(
                f"{'#' * level} {name}" for level, name in enumerate(heading, 1)
            )
            if heading:
                paths.append(list(heading))
            previous_heading = heading
        parts.append(body)
    markdown = "\n\n".join(parts).strip()
    plain = _LINK.sub(r"\1", markdown)
    plain = _HEADING_MARKER.sub("", plain)
    plain = _MARKUP.sub("", plain)
    plain = normalize_text(plain)
    return plain, markdown, json.dumps(paths, ensure_ascii=False)


def chunk_markdown(
    markdown: str,
    *,
    title: str | None = None,
    target_words: int = 500,
    overlap_words: int = 75,
) -> list[tuple[str, str, str]]:
    """Return `(plain_text, markdown, heading_paths_json)` chunks."""
    if target_words < 1 or overlap_words < 0 or overlap_words >= target_words:
        raise ValueError("require 0 <= overlap_words < target_words")
    sections = _MARKDOWN_SPLITTER.split_text(_demote_oversized_headings(markdown))
    segments = []
    for section in sections:
        heading = tuple(
            section.metadata[key] for _, key in _HEADERS if section.metadata.get(key)
        )
        if not heading and title and _word_count(title) <= 30:
            heading = (title,)
        body_size = target_words - sum(_word_count(part) for part in heading)
        segments.extend(
            (heading, body)
            for body in _recursive_split(section.page_content, body_size, overlap_words)
            if body.strip()
        )

    groups = []
    current = []
    current_words = 0
    for heading, body in segments:
        heading_words = (
            sum(_word_count(part) for part in heading)
            if not current or current[-1][0] != heading
            else 0
        )
        segment_words = heading_words + _word_count(body)
        if current and current_words + segment_words > target_words:
            groups.append(current)
            current = []
            current_words = 0
            segment_words = sum(_word_count(part) for part in heading) + _word_count(
                body
            )
        current.append((heading, body))
        current_words += segment_words
    if current:
        groups.append(current)
    return [_render(group) for group in groups]


def _fallback_chunks(
    text: str, *, title: str | None, target_words: int
) -> list[tuple[str, str, str]]:
    """Chunk heading-only/degenerate Markdown without dropping the document."""
    heading = (title,) if title and _word_count(title) <= 30 else ()
    body_limit = target_words - sum(_word_count(part) for part in heading)
    return [
        _render([(heading, piece)])
        for piece in _recursive_split(text, body_limit, 0)
        if piece.strip()
    ]


def _chunk_id(document_id: str, content_hash: str, index: int) -> str:
    raw = f"{document_id}:{content_hash}:{index}:{CHUNKER_VERSION}".encode()
    return hashlib.sha256(raw).hexdigest()[:24]


def _state_hash(values: list[object]) -> str:
    raw = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _document_content_hash(row: sqlite3.Row, document_hash: str) -> str:
    """Hash only fields that can alter rendered chunk text or boundaries."""
    return _state_hash([document_hash, row["markdown"], row["title"]])


def _document_metadata_hash(row: sqlite3.Row) -> str:
    """Hash copied retrieval metadata without invalidating chunk content."""
    return _state_hash(
        [
            row[key]
            for key in (
                "url",
                "title",
                "site",
                "source_type",
                "lang",
                "revision",
                "metadata",
                "standort_id",
                "department_id",
                "study_program_id",
                "classify_meta",
            )
        ]
    )


def run_chunking(
    conn: sqlite3.Connection,
    *,
    target_words: int = 500,
    overlap_words: int = 75,
    batch_size: int = 250,
) -> dict[str, int]:
    """Incrementally synchronize `document_chunks` with present documents."""
    if overlap_words >= target_words:
        raise ValueError("chunk.overlap_words must be smaller than chunk.target_words")
    existing = {
        row["document_id"]: (
            row["document_text_sha256"],
            row["document_content_sha256"],
            row["document_metadata_sha256"],
            row["chunker_version"],
            row["target_words"],
            row["overlap_words"],
        )
        for row in conn.execute(
            """SELECT document_id, MIN(document_text_sha256) document_text_sha256,
                      MIN(document_content_sha256) document_content_sha256,
                      MIN(document_metadata_sha256) document_metadata_sha256,
                      MIN(chunker_version) chunker_version,
                      MIN(target_words) target_words, MIN(overlap_words) overlap_words
               FROM document_chunks GROUP BY document_id"""
        )
    }
    deleted = conn.execute(
        """DELETE FROM document_chunks
           WHERE NOT EXISTS (
               SELECT 1 FROM documents d
               WHERE d.id = document_chunks.document_id AND d.present = 1
           )"""
    ).rowcount
    conn.commit()

    result = {
        "documents": 0,
        "metadata_updated": 0,
        "unchanged": 0,
        "chunks": 0,
        "deleted": deleted,
    }
    last_id = ""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    while True:
        states = conn.execute(
            """SELECT id, text_sha256, markdown, url, title, site, source_type,
                      lang, revision, metadata, standort_id, department_id,
                      study_program_id, classify_meta
               FROM documents
               WHERE present = 1 AND id > ? ORDER BY id LIMIT ?""",
            (last_id, batch_size),
        ).fetchall()
        if not states:
            break
        changed_ids: list[str] = []
        metadata_ids: list[str] = []
        for state in states:
            document_hash = state["text_sha256"]
            if document_hash is None:
                text = conn.execute(
                    "SELECT text FROM documents WHERE id = ?", (state["id"],)
                ).fetchone()["text"]
                document_hash = text_hash(text)
            content_signature = (
                document_hash,
                _document_content_hash(state, document_hash),
                CHUNKER_VERSION,
                target_words,
                overlap_words,
            )
            current = existing.get(state["id"])
            if (
                current is None
                or (current[0], current[1], current[3], current[4], current[5])
                != content_signature
            ):
                changed_ids.append(state["id"])
            elif current[2] != _document_metadata_hash(state):
                metadata_ids.append(state["id"])
            else:
                result["unchanged"] += 1
        action_ids = changed_ids + metadata_ids
        placeholders = ",".join("?" for _ in action_ids)
        rows = (
            conn.execute(
                f"SELECT * FROM documents WHERE id IN ({placeholders}) ORDER BY id",
                action_ids,
            ).fetchall()
            if action_ids
            else []
        )
        with write_txn(conn):
            for row in rows:
                document_hash = row["text_sha256"] or text_hash(row["text"])
                content_hash = _document_content_hash(row, document_hash)
                metadata_hash = _document_metadata_hash(row)
                if row["id"] in metadata_ids:
                    conn.execute(
                        """UPDATE document_chunks SET
                               url=?, title=?, site=?, source_type=?, lang=?,
                               document_revision=?, metadata=?, standort_id=?,
                               department_id=?, study_program_id=?, classify_meta=?,
                               document_metadata_sha256=?
                           WHERE document_id=?""",
                        (
                            row["url"],
                            row["title"],
                            row["site"],
                            row["source_type"],
                            row["lang"],
                            row["revision"],
                            row["metadata"],
                            row["standort_id"],
                            row["department_id"],
                            row["study_program_id"],
                            row["classify_meta"],
                            metadata_hash,
                            row["id"],
                        ),
                    )
                    result["metadata_updated"] += 1
                    continue
                conn.execute(
                    "DELETE FROM document_chunks WHERE document_id = ?", (row["id"],)
                )
                chunks = chunk_markdown(
                    row["markdown"],
                    title=row["title"],
                    target_words=target_words,
                    overlap_words=overlap_words,
                )
                if not chunks:
                    chunks = _fallback_chunks(
                        row["text"], title=row["title"], target_words=target_words
                    )
                payload = []
                for index, (plain, markdown, headings) in enumerate(chunks):
                    payload.append(
                        (
                            _chunk_id(row["id"], content_hash, index),
                            row["id"],
                            index,
                            row["url"],
                            row["title"],
                            row["site"],
                            row["source_type"],
                            row["lang"],
                            plain,
                            markdown,
                            headings,
                            len(_WORDS.findall(plain)),
                            len(plain),
                            text_hash(plain),
                            document_hash,
                            content_hash,
                            metadata_hash,
                            row["revision"],
                            row["metadata"],
                            row["standort_id"],
                            row["department_id"],
                            row["study_program_id"],
                            row["classify_meta"],
                            CHUNKER_VERSION,
                            target_words,
                            overlap_words,
                            now,
                        )
                    )
                conn.executemany(
                    """INSERT INTO document_chunks (
                           id, document_id, chunk_index, url, title, site, source_type,
                           lang, text, markdown, heading_path, word_count, char_count,
                           content_sha256, document_text_sha256,
                           document_content_sha256, document_metadata_sha256,
                           document_revision,
                           metadata, standort_id, department_id, study_program_id,
                           classify_meta, chunker_version, target_words, overlap_words,
                           created_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    payload,
                )
                result["documents"] += 1
                result["chunks"] += len(payload)
        last_id = states[-1]["id"]
    return result
