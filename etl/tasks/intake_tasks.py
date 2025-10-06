"""Prefect task implementations for the ingestion pipeline skeleton.

These implementations currently provide lightweight placeholder behaviour so
that the end-to-end flow can be exercised without external services. Later
work will replace the heuristics with real Docling, Gemini, Voyage, and
storage integrations.
"""

from __future__ import annotations

import json
from datetime import datetime
import logging
import re
import time
from typing import Dict, List, Optional, Sequence
from uuid import uuid4

import httpx
from prefect import task
from sqlalchemy import text

from .intake_models import (
    AcquiredSource,
    Chunk,
    ChunkEmbedding,
    FlowReport,
    IngestItem,
    NormalizedDocument,
    new_ingest_item,
)
from . import chunker
from .model_clients import (
    ClassificationResult,
    classify_domain,
    embed_with_gemini,
    embed_with_voyage,
    summarize_chunks_with_gemini,
    summarize_with_gemini,
)
from .ner import extract_ner_tags
from src.my_agentic_chatbot.config import get_settings
from src.my_agentic_chatbot.storage.db import get_engine
from urllib.parse import urlparse

LOGGER = logging.getLogger(__name__)

CHUNK_TOKENS_DEFAULT = 500
OVERLAP_MAX_PCT_DEFAULT = 0.15


@task
def register_ingest_item(
    source_type: str,
    source_uri: str,
    display_name: str,
    metadata: Optional[Dict[str, object]] = None,
) -> IngestItem:
    """Create a new ingest item record."""

    item = new_ingest_item(source_type, source_uri, display_name, metadata)
    item.status = "processing"
    item.metadata.setdefault("job_id", str(item.job_id))
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO kb.ingest_items (id, job_id, source_type, source_uri, display_name, status, metadata)
                VALUES (:id, :job_id, :source_type, :source_uri, :display_name, :status, CAST(:metadata AS JSONB))
                ON CONFLICT (id) DO UPDATE
                SET source_type = EXCLUDED.source_type,
                    source_uri = EXCLUDED.source_uri,
                    display_name = EXCLUDED.display_name,
                    status = EXCLUDED.status,
                    metadata = EXCLUDED.metadata,
                    updated_at = NOW()
                """
            ),
            {
                "id": str(item.id),
                "job_id": str(item.job_id) if item.job_id else None,
                "source_type": item.source_type,
                "source_uri": item.source_uri,
                "display_name": item.display_name,
                "status": item.status,
                "metadata": json.dumps(item.metadata),
            },
        )
    LOGGER.debug("Registered ingest item %s", item.id)
    return item


@task
def acquire_source(item: IngestItem, content: Optional[bytes]) -> AcquiredSource:
    """Acquire raw bytes for the ingest item, fetching remote sources if needed."""

    settings = get_settings()
    metadata: Dict[str, object] = dict(item.metadata)
    metadata.setdefault("source_type", item.source_type)
    metadata.setdefault("source_uri", item.source_uri)
    metadata.setdefault("display_name", item.display_name)

    if content is None:
        if not item.source_uri:
            raise RuntimeError("No content bytes provided and source URI is empty")
        content, fetch_meta = _fetch_remote_content(
            item.source_uri,
            timeout=settings.ingest_http_timeout_seconds,
        )
        metadata.update(fetch_meta)
    else:
        metadata.setdefault("content_length", len(content))

    if not content:
        raise RuntimeError(f"Ingest item {item.id} resolved to empty content")

    metadata.setdefault("filename", _derive_filename(item, metadata))
    metadata.setdefault("content_length", len(content))
    if not metadata.get("content_type"):
        metadata["content_type"] = "application/octet-stream"
    metadata.setdefault("ingested_at", _iso_now())

    LOGGER.debug("Acquired %d bytes for ingest item %s", len(content), item.id)
    return AcquiredSource(ingest_item_id=item.id, content=content, metadata=metadata)


@task
def docling_normalize(source: AcquiredSource) -> NormalizedDocument:
    """Normalize source bytes via Docling, falling back to raw decoding on failure."""

    settings = get_settings()
    filename = str(source.metadata.get("filename") or "document")
    content_type = str(source.metadata.get("content_type") or "application/octet-stream")

    try:
        markdown, plain_text, docling_meta = _docling_convert(
            source.content,
            filename=filename,
            content_type=content_type,
            timeout=settings.docling_timeout_seconds,
            poll_interval=settings.docling_poll_interval_seconds,
            base_url=settings.docling_base_url,
        )
        text = plain_text or _markdown_to_text(markdown)
        metadata: Dict[str, object] = {
            "language": docling_meta.get("language", "und"),
            "docling": docling_meta,
            "source": dict(source.metadata),
        }
    except DoclingConversionError as exc:
        LOGGER.error(
            "Docling conversion failed for %s (%s): %s",
            source.ingest_item_id,
            filename,
            exc,
        )
        text = source.content.decode("utf-8", errors="replace")
        markdown = text
        metadata = {
            "language": "und",
            "docling_error": str(exc),
            "source": dict(source.metadata),
        }

    return NormalizedDocument(
        ingest_item_id=source.ingest_item_id,
        markdown=markdown,
        text=text,
        metadata=metadata,
    )


@task
def classify_domain_task(item: IngestItem, document: NormalizedDocument) -> IngestItem:
    """Classify the document domain using Gemini 2.5 Flash via LiteLLM."""

    try:
        result: ClassificationResult = classify_domain(document.text)
    except RuntimeError as exc:
        LOGGER.error("Gemini classification failed for %s: %s", item.id, exc)
        result = ClassificationResult(domain="general", confidence=0.0, source_labels=[])

    updated = item.copy()
    updated.domain = result.domain
    updated.domain_confidence = result.confidence
    updated.metadata.setdefault("classification", {})
    updated.metadata["classification"].update(
        {
            "source_labels": result.source_labels,
        }
    )
    LOGGER.debug(
        "Classified ingest item %s as %s (confidence=%.3f)",
        item.id,
        updated.domain,
        updated.domain_confidence or 0.0,
    )
    return updated


@task
def summarize_document(item: IngestItem, document: NormalizedDocument) -> IngestItem:
    """Produce a short document-level summary using heuristics.

    Gemini 2.5 Flash will later replace this with an LLM call. We keep the
    output short so it can seed the UI even before full processing finishes.
    """

    summary = summarize_with_gemini(document.text, max_length=500)
    updated = item.copy()
    updated.document_summary = summary
    LOGGER.debug("Stored document summary for %s", item.id)
    return updated


@task
def chunk_and_ner(item: IngestItem, document: NormalizedDocument) -> List[Chunk]:
    """Produce placeholder chunks and empty NER metadata."""

    chunk_defs = chunker.build_chunks(
        document.markdown,
        CHUNK_TOKENS_DEFAULT,
        OVERLAP_MAX_PCT_DEFAULT,
    )
    chunks: List[Chunk] = []
    for chunk_def in chunk_defs:
        idx = int(chunk_def["idx"])
        chunk_text = str(chunk_def["text"])
        heading = str(chunk_def.get("title", "Document"))
        ner_entities = extract_ner_tags(chunk_text)
        chunk = Chunk(
            id=uuid4(),
            ingest_item_id=item.id,
            document_id=None,
            chunk_index=idx,
            heading_path=[heading],
            kind="paragraph",
            text=chunk_text,
            token_count=int(chunk_def.get("token_count", chunker.rough_tokens(chunk_text))),
            overlap_tokens=int(chunk_def.get("overlap", 0)),
            ner_entities=ner_entities,
            summary=None,
        )
        chunks.append(chunk)
    LOGGER.debug(
        "Chunked ingest item %s into %d chunk(s) with %d ner tags",
        item.id,
        len(chunks),
        sum(len(chunk.ner_entities) for chunk in chunks),
    )
    return chunks


@task
def summarize_chunks(item: IngestItem, chunks: List[Chunk]) -> IngestItem:
    """Assign naive chunk summaries."""

    updated = item.copy()
    summaries = summarize_chunks_with_gemini([chunk.text for chunk in chunks], max_length=256)
    for chunk, summary in zip(chunks, summaries):
        chunk.summary = summary
        updated.chunk_summaries[chunk.chunk_index] = summary
    LOGGER.debug("Generated %d chunk summary entries for %s", len(chunks), item.id)
    return updated


@task
def build_budgeted_abstractions(chunks: List[Chunk]) -> List[str]:
    """Return coarse abstractions used when evidence budgets overflow."""

    # Placeholder: collapse neighbouring summaries into paragraphs.
    abstractions: List[str] = []
    group: List[str] = []
    for chunk in chunks:
        group.append(chunk.summary or chunk.text)
        if len(group) == 3:
            abstractions.append(" ".join(group))
            group = []
    if group:
        abstractions.append(" ".join(group))
    return abstractions


@task
def embed_chunks(item: IngestItem, chunks: List[Chunk]) -> List[ChunkEmbedding]:
    """Produce dummy embeddings so downstream plumbing works."""

    embeddings: List[ChunkEmbedding] = []
    texts = [chunk.text for chunk in chunks]
    general_vectors = embed_with_gemini(texts)
    for vector, chunk in zip(general_vectors, chunks):
        embeddings.append(
            ChunkEmbedding(
                chunk_id=chunk.id,
                space="general",
                model="gemini/text-embedding-004",
                vector=vector,
            )
        )

    if (item.domain or "general") == "legal":
        legal_vectors = embed_with_voyage(texts, model="voyage-law-2")
        for vector, chunk in zip(legal_vectors, chunks):
            embeddings.append(
                ChunkEmbedding(
                    chunk_id=chunk.id,
                    space="legal",
                    model="voyage-law-2",
                    vector=vector,
                )
            )
    elif (item.domain or "general") == "code":
        code_vectors = embed_with_voyage(texts, model="voyage-code-3")
        for vector, chunk in zip(code_vectors, chunks):
            embeddings.append(
                ChunkEmbedding(
                    chunk_id=chunk.id,
                    space="code",
                    model="voyage-code-3",
                    vector=vector,
                )
            )
    LOGGER.debug("Generated %d embedding payload(s) for %s", len(embeddings), item.id)
    return embeddings


@task
def persist_results(
    item: IngestItem,
    document: NormalizedDocument,
    chunks: List[Chunk],
    embeddings: List[ChunkEmbedding],
    abstractions: List[str],
) -> FlowReport:
    """Persist the enriched document, chunks, and embeddings into ParadeDB."""

    engine = get_engine()
    source_meta = document.metadata.get("source", {}) if isinstance(document.metadata, dict) else {}
    docling_meta = document.metadata.get("docling") if isinstance(document.metadata, dict) else None
    language = str(document.metadata.get("language", "und")) if isinstance(document.metadata, dict) else "und"
    tsvector_config = _tsvector_config(language)

    document_id: Optional[str] = None
    if item.source_type == "document":
        document_id = str(uuid4())

    ingest_metadata = dict(item.metadata)
    ingest_metadata.update(
        {
            "document_metadata": document.metadata,
            "source": source_meta,
            "chunk_abstractions": abstractions,
            "chunk_summaries": item.chunk_summaries,
        }
    )

    chunk_mappings: List[Dict[str, object]] = []
    for chunk in chunks:
        mapping: Dict[str, object] = {
            "id": str(chunk.id),
            "ingest_item_id": str(chunk.ingest_item_id),
            "document_id": str(document_id) if document_id else None,
            "chunk_index": chunk.chunk_index,
            "heading_path": chunk.heading_path,
            "kind": chunk.kind,
            "text": chunk.text,
            "summary": chunk.summary,
            "token_count": chunk.token_count,
            "overlap_tokens": chunk.overlap_tokens,
            "ner_entities": json.dumps(chunk.ner_entities),
            "metadata": json.dumps({"source_type": item.source_type}),
        }
        chunk_mappings.append(mapping)

    embedding_spaces: Dict[str, int] = {}
    vector_payloads: List[Dict[str, object]] = []

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE kb.ingest_items
                SET domain = :domain,
                    domain_confidence = :domain_confidence,
                    document_summary = :document_summary,
                    metadata = CAST(:metadata AS JSONB),
                    mime_type = COALESCE(:mime_type, mime_type),
                    language = :language,
                    updated_at = NOW()
                WHERE id = :id
                """
            ),
            {
                "id": str(item.id),
                "domain": item.domain,
                "domain_confidence": item.domain_confidence,
                "document_summary": item.document_summary,
                "metadata": json.dumps(ingest_metadata),
                "mime_type": source_meta.get("content_type"),
                "language": language,
            },
        )

        if document_id:
            conn.execute(
                text(
                    """
                    INSERT INTO kb.documents (id, ingest_item_id, title, text_full, tsv, metadata)
                    VALUES (:id, :ingest_item_id, :title, :text_full, to_tsvector(:ts_config, :text_full), CAST(:metadata AS JSONB))
                    ON CONFLICT (id) DO UPDATE
                    SET title = EXCLUDED.title,
                        text_full = EXCLUDED.text_full,
                        tsv = EXCLUDED.tsv,
                        metadata = EXCLUDED.metadata
                    """
                ),
                {
                    "id": document_id,
                    "ingest_item_id": str(item.id),
                    "title": document.metadata.get("docling", {}).get("title") if isinstance(document.metadata, dict) else item.display_name,
                    "text_full": document.text,
                    "ts_config": tsvector_config,
                    "metadata": json.dumps(
                        {
                            "docling": docling_meta,
                            "source": source_meta,
                        }
                    ),
                },
            )

        for mapping in chunk_mappings:
            conn.execute(
                text(
                    """
                    INSERT INTO kb.chunks (id, ingest_item_id, document_id, chunk_index, heading_path, kind, text, summary, token_count, overlap_tokens, ner_entities, tsv, metadata)
                    VALUES (:id, :ingest_item_id, :document_id, :chunk_index, :heading_path, :kind, :text, :summary, :token_count, :overlap_tokens, CAST(:ner_entities AS JSONB), to_tsvector(:ts_config, :text), CAST(:metadata AS JSONB))
                    ON CONFLICT (id) DO UPDATE
                    SET text = EXCLUDED.text,
                        summary = EXCLUDED.summary,
                        token_count = EXCLUDED.token_count,
                        overlap_tokens = EXCLUDED.overlap_tokens,
                        ner_entities = EXCLUDED.ner_entities,
                        tsv = EXCLUDED.tsv,
                        metadata = EXCLUDED.metadata
                    """
                ),
                {
                    **mapping,
                    "ts_config": tsvector_config,
                },
            )

        for embedding_payload in embeddings:
            space_id = embedding_spaces.get(embedding_payload.space)
            if space_id is None:
                space_id = _ensure_embedding_space(
                    conn,
                    embedding_payload.space,
                    embedding_payload.model,
                    len(embedding_payload.vector),
                )
                embedding_spaces[embedding_payload.space] = space_id
            vector_literal = _vector_literal(embedding_payload.vector)
            vector_payloads.append(
                {
                    "chunk_id": str(embedding_payload.chunk_id),
                    "space_id": space_id,
                    "embedding": vector_literal,
                }
            )

        for payload in vector_payloads:
            conn.execute(
                text(
                    """
                    INSERT INTO kb.chunk_embeddings (chunk_id, space_id, embedding)
                    VALUES (:chunk_id, :space_id, CAST(:embedding AS vector))
                    ON CONFLICT (chunk_id, space_id) DO UPDATE
                    SET embedding = EXCLUDED.embedding,
                        created_at = NOW()
                    """
                ),
                payload,
            )

    ner_total = sum(len(chunk.ner_entities) for chunk in chunks)
    report_metadata: Dict[str, object] = {
        "abstraction_count": len(abstractions),
        "embedding_count": len(embeddings),
        "chunk_count": len(chunks),
        "ner_entity_total": ner_total,
        "document_id": document_id,
    }

    item.metadata = ingest_metadata

    report = FlowReport(
        ingest_item=item.copy(),
        chunk_count=len(chunks),
        embedding_spaces=sorted(embedding_spaces.keys()),
        job_id=str(item.job_id) if item.job_id else None,
        metadata=report_metadata,
    )
    LOGGER.debug("Persisted ingest item %s to knowledge base", item.id)
    return report


@task
def mirror_openwebui(item: IngestItem) -> None:
    """Mirror document ingests into Open WebUI's file library."""

    settings = get_settings()
    db_url = settings.openwebui_database_url
    if not db_url:
        LOGGER.debug("Open WebUI database URL not configured; skipping mirror")
        return

    try:
        engine = get_engine(db_url)
    except Exception as exc:  # pragma: no cover - connectivity issue
        LOGGER.warning("Unable to initialise Open WebUI engine: %s", exc)
        return

    source_meta = item.metadata.get("source", {}) if isinstance(item.metadata, dict) else {}
    filename = source_meta.get("filename") or item.display_name
    path_hint = f"ingest://{item.id}"
    now_epoch = int(time.time())
    data_payload = json.dumps(
        {
            "ingest_item_id": str(item.id),
            "domain": item.domain,
        }
    )
    meta_payload = json.dumps(
        {
            "ingest_item_id": str(item.id),
            "source_type": item.source_type,
            "mime_type": source_meta.get("content_type"),
            "content_length": source_meta.get("content_length"),
            "summary": item.document_summary,
        }
    )

    dialect = engine.dialect.name
    try:
        with engine.begin() as conn:
            if dialect == "sqlite":
                conn.execute(
                    text(
                        """
                        INSERT OR REPLACE INTO file (id, user_id, hash, filename, path, data, meta, created_at, updated_at)
                        VALUES (:id, :user_id, :hash, :filename, :path, :data, :meta, :created_at, :updated_at)
                        """
                    ),
                    {
                        "id": str(item.id),
                        "user_id": "system",
                        "hash": None,
                        "filename": filename,
                        "path": path_hint,
                        "data": data_payload,
                        "meta": meta_payload,
                        "created_at": now_epoch,
                        "updated_at": now_epoch,
                    },
                )
            else:
                conn.execute(
                    text(
                        """
                        INSERT INTO file (id, user_id, hash, filename, path, data, meta, created_at, updated_at)
                        VALUES (:id, :user_id, :hash, :filename, :path, :data::jsonb, :meta::jsonb, :created_at, :updated_at)
                        ON CONFLICT (id) DO UPDATE
                        SET filename = EXCLUDED.filename,
                            path = EXCLUDED.path,
                            data = EXCLUDED.data,
                            meta = EXCLUDED.meta,
                            updated_at = EXCLUDED.updated_at
                        """
                    ),
                    {
                        "id": str(item.id),
                        "user_id": "system",
                        "hash": None,
                        "filename": filename,
                        "path": path_hint,
                        "data": data_payload,
                        "meta": meta_payload,
                        "created_at": now_epoch,
                        "updated_at": now_epoch,
                    },
                )
    except Exception as exc:  # pragma: no cover - dependent on external DB
        LOGGER.warning("Failed to mirror ingest item %s to Open WebUI: %s", item.id, exc)


@task
def finalize_status(item: IngestItem, status: str) -> IngestItem:
    """Return a copy of the ingest item with the provided status."""

    updated = item.copy()
    updated.status = status
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE kb.ingest_items
                SET status = :status,
                    updated_at = NOW()
                WHERE id = :id
                """
            ),
            {"status": status, "id": str(item.id)},
        )
    LOGGER.debug("Finalized ingest item %s with status %s", item.id, status)
    return updated


# ---------------------------------------------------------------------------
# Helper utilities


class DoclingConversionError(RuntimeError):
    """Raised when the Docling service fails to convert the source."""


class DoclingTimeoutError(DoclingConversionError):
    """Raised when Docling conversion exceeds the configured timeout."""


def _fetch_remote_content(url: str, *, timeout: float) -> tuple[bytes, Dict[str, object]]:
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as exc:  # pragma: no cover - exercised via mocks
        raise RuntimeError(f"Failed to fetch source '{url}': {exc}") from exc

    if response.status_code >= 400:
        raise RuntimeError(
            f"Fetching source '{url}' returned HTTP {response.status_code}"
        )

    content = response.content
    metadata: Dict[str, object] = {
        "http_status": response.status_code,
        "fetched_url": str(response.url),
        "content_length": len(content),
        "content_type": response.headers.get("content-type"),
        "fetched_at": _iso_now(),
    }
    if "filename" not in metadata:
        metadata["filename"] = _filename_from_url(str(response.url))
    return content, metadata


def _derive_filename(item: IngestItem, metadata: Dict[str, object]) -> str:
    if isinstance(metadata.get("filename"), str) and metadata["filename"]:
        return str(metadata["filename"])

    if item.source_uri:
        candidate = _filename_from_url(item.source_uri)
        if candidate:
            return candidate

    display = (item.display_name or "document").strip()
    return display or "document"


def _filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rsplit("/", 1)[-1] if parsed.path else ""
    return path or parsed.netloc or "document"


def _iso_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _docling_convert(
    content: bytes,
    *,
    filename: str,
    content_type: str,
    timeout: float,
    poll_interval: float,
    base_url: str,
) -> tuple[str, Optional[str], Dict[str, object]]:
    client_kwargs = {"timeout": timeout}
    base = base_url.rstrip("/")
    with httpx.Client(**client_kwargs) as client:
        try:
            payload = _docling_convert_sync(
                client,
                base,
                filename,
                content,
                content_type,
                timeout,
            )
        except DoclingTimeoutError:
            LOGGER.info("Docling sync conversion timed out for %s; retrying async", filename)
            payload = _docling_convert_async(
                client,
                base,
                filename,
                content,
                content_type,
                timeout,
                poll_interval,
            )

    markdown, plain_text, metadata = _extract_docling_result(payload)
    return markdown, plain_text, metadata


def _docling_convert_sync(
    client: httpx.Client,
    base_url: str,
    filename: str,
    content: bytes,
    content_type: str,
    timeout: float,
) -> Dict[str, object]:
    files = {"files": (filename, content, content_type or "application/octet-stream")}
    data = {"to_formats": "md"}
    url = f"{base_url}/v1/convert/file"
    try:
        response = client.post(url, files=files, data=data, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise DoclingTimeoutError("Docling synchronous conversion timed out") from exc
    except httpx.HTTPError as exc:
        raise DoclingConversionError(f"Docling request failed: {exc}") from exc

    if response.status_code == 504:
        raise DoclingTimeoutError("Docling synchronous conversion returned 504")
    if response.status_code >= 400:
        raise DoclingConversionError(
            f"Docling request failed with status {response.status_code}: {response.text}"
        )

    try:
        return response.json()
    except ValueError as exc:
        raise DoclingConversionError("Docling response was not valid JSON") from exc


def _docling_convert_async(
    client: httpx.Client,
    base_url: str,
    filename: str,
    content: bytes,
    content_type: str,
    timeout: float,
    poll_interval: float,
) -> Dict[str, object]:
    files = {"files": (filename, content, content_type or "application/octet-stream")}
    data = {"to_formats": "md"}
    submit_url = f"{base_url}/v1/convert/file/async"
    try:
        response = client.post(submit_url, files=files, data=data, timeout=timeout)
        response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise DoclingTimeoutError("Docling async submission timed out") from exc
    except httpx.HTTPStatusError as exc:
        raise DoclingConversionError(
            f"Docling async request failed with {exc.response.status_code}: {exc.response.text}"
        ) from exc
    except httpx.HTTPError as exc:
        raise DoclingConversionError(f"Docling async request failed: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise DoclingConversionError("Docling async submission response was not JSON") from exc

    task_id = payload.get("task_id")
    if not task_id:
        raise DoclingConversionError("Docling async response did not include a task_id")

    result_payload = _poll_docling_task(
        client,
        base_url,
        task_id,
        timeout,
        poll_interval,
    )
    return result_payload


def _poll_docling_task(
    client: httpx.Client,
    base_url: str,
    task_id: str,
    timeout: float,
    poll_interval: float,
) -> Dict[str, object]:
    deadline = time.monotonic() + timeout
    status_url = f"{base_url}/v1/status/poll/{task_id}"
    result_url = f"{base_url}/v1/result/{task_id}"

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DoclingTimeoutError("Docling async conversion timed out")
        wait = min(poll_interval, max(0.0, remaining))
        try:
            response = client.get(status_url, params={"wait": wait}, timeout=wait + 5)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise DoclingTimeoutError("Docling status polling timed out") from exc
        except httpx.HTTPStatusError as exc:
            raise DoclingConversionError(
                f"Docling status polling failed with {exc.response.status_code}: {exc.response.text}"
            ) from exc
        except httpx.HTTPError as exc:
            raise DoclingConversionError(f"Docling status polling failed: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise DoclingConversionError("Docling status response was not JSON") from exc

        task_status = payload.get("task_status")
        if task_status in {"success", "partial_success"}:
            break
        if task_status in {"failure", "skipped"}:
            raise DoclingConversionError(
                f"Docling async conversion failed with status '{task_status}'"
            )

    try:
        result_response = client.get(result_url, timeout=timeout)
        result_response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise DoclingTimeoutError("Docling result fetch timed out") from exc
    except httpx.HTTPStatusError as exc:
        raise DoclingConversionError(
            f"Docling result fetch failed with {exc.response.status_code}: {exc.response.text}"
        ) from exc
    except httpx.HTTPError as exc:
        raise DoclingConversionError(f"Docling result fetch failed: {exc}") from exc

    try:
        return result_response.json()
    except ValueError as exc:
        raise DoclingConversionError("Docling result response was not JSON") from exc


def _extract_docling_result(payload: Dict[str, object]) -> tuple[str, Optional[str], Dict[str, object]]:
    document = payload.get("document") or {}
    markdown = document.get("md_content")
    if not markdown:
        raise DoclingConversionError("Docling response did not contain markdown content")

    metadata = {
        "docling_status": payload.get("status"),
        "docling_errors": payload.get("errors") or [],
        "language": document.get("language", "und"),
        "title": document.get("title") or (document.get("metadata") or {}).get("title"),
        "toc": document.get("toc"),
        "docling_metadata": document.get("metadata"),
    }
    plain_text = document.get("plain_text") or document.get("txt_content")
    return markdown, plain_text, metadata


def _markdown_to_text(markdown: str) -> str:
    lines: List[str] = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            lines.append("")
            continue
        line = re.sub(r"^#{1,6}\s+", "", line)
        line = line.replace("**", "").replace("__", "")
        line = line.replace("*", "").replace("_", "")
        line = line.replace("`", "")
        lines.append(line)
    return "\n".join(lines)



def _ensure_embedding_space(
    conn,
    name: str,
    model: str,
    dims: int,
) -> int:
    existing = conn.execute(
        text("SELECT id FROM kb.embedding_spaces WHERE name = :name"),
        {"name": name},
    ).fetchone()
    if existing:
        return existing[0]

    provider = "google-gemini" if "gemini" in model or name == "general" else "voyage"
    distance = "cosine"
    result = conn.execute(
        text(
            """
            INSERT INTO kb.embedding_spaces (name, model, provider, dims, distance_metric)
            VALUES (:name, :model, :provider, :dims, :distance_metric)
            RETURNING id
            """
        ),
        {
            "name": name,
            "model": model,
            "provider": provider,
            "dims": dims,
            "distance_metric": distance,
        },
    )
    return result.scalar_one()


def _vector_literal(values: Sequence[float]) -> str:
    return "[" + ",".join(f"{value:.10f}" for value in values) + "]"


def _tsvector_config(language: str) -> str:
    if language.lower().startswith("en"):
        return "english"
    return "simple"
