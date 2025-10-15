"""Prefect task implementations for the ingestion pipeline skeleton.

These implementations currently provide lightweight placeholder behaviour so
that the end-to-end flow can be exercised without external services. Later
work will replace the heuristics with real Docling, Gemini, Voyage, and
storage integrations.
"""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
import logging
import re
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from uuid import UUID, uuid4

import httpx
try:
    from prefect import task
except ModuleNotFoundError:  # pragma: no cover - prefect optional for tests
    def task(function):
        return function
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
from . import chunker, task_registry
from .model_clients import (
    ClassificationResult,
    analyze_emotions,
    classify_domain,
    embed_with_code,
    embed_with_general,
    embed_with_legal,
    summarize_chunks_with_gemini,
    summarize_with_gemini,
)
from .ner import extract_ner_tags
from src.my_agentic_chatbot.config import get_settings
from src.my_agentic_chatbot.runtime_config import (
    ModelConfig,
    get_runtime_config,
    get_tool_config,
)
from src.my_agentic_chatbot.storage.connection import get_engine

try:  # pragma: no cover - optional when running outside Freshbot environment
    from src.freshbot.registry import flag_registry  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - tests may run without Freshbot package
    class _FlagRegistryStub:  # type: ignore[too-few-public-methods]
        @staticmethod
        def resolve_flags(source_metadata=None, overrides=None):
            return {}

        @staticmethod
        def active_flag_names(resolved_flags):
            return []

    flag_registry = _FlagRegistryStub()  # type: ignore[assignment]

try:  # pragma: no cover - optional when running outside Freshbot environment
    from src.freshbot import references as reference_utils  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - tests may run without Freshbot package
    class _ReferenceUtilsStub:  # type: ignore[too-few-public-methods]
        @staticmethod
        def extract_reference_ids(source_metadata=None):
            return []

    reference_utils = _ReferenceUtilsStub()  # type: ignore[assignment]
from urllib.parse import urlparse

try:
    from freshbot.executors import prefect as freshbot_prefect
except Exception:  # pragma: no cover - executor optional in unit tests
    freshbot_prefect = None

LOGGER = logging.getLogger(__name__)

TEI_MAX_INPUT_TOKENS = 512  # retrieved from /info on the gte-large TEI container
CHUNK_TOKENS_DEFAULT = int(TEI_MAX_INPUT_TOKENS * 0.88)  # stay ~12% under the max (450 tokens)
OVERLAP_MAX_PCT_DEFAULT = 0.15
# GTE-large handles long inputs comfortably; keep parity with the previous 30k char cap.
GENERAL_EMBED_CHAR_LIMIT = 30000
EMOTION_PREVIEW_CHAR_LIMIT = 600


METADATA_FLOW_DEPLOYMENT = "freshbot_metadata_flag/freshbot-metadata-flag"


def _iter_flags(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _iter_flags(item)
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        for item in value:
            yield from _iter_flags(item)


def _trigger_metadata_flag_flows(
    *,
    entry_id: str,
    ingest_item_id: str,
    flags: Sequence[str],
    context: Mapping[str, Any],
) -> None:
    if not flags:
        return
    if freshbot_prefect is None:
        LOGGER.warning(
            "Metadata flag detected but Freshbot executor unavailable",
            extra={"entry_id": entry_id, "flags": list(flags)},
        )
        return
    for flag in flags:
        try:
            freshbot_prefect.execute_flow(
                METADATA_FLOW_DEPLOYMENT,
                parameters={
                    "flag": flag,
                    "entry_id": entry_id,
                    "ingest_item_id": ingest_item_id,
                    "context": dict(context),
                },
            )
        except Exception as exc:  # pragma: no cover - logging best effort
            LOGGER.warning(
                "Metadata flow trigger failed",
                extra={
                    "entry_id": entry_id,
                    "flag": flag,
                    "error": str(exc),
                },
            )


def _chunk_text_hash(text: str) -> str:
    data = (text or "").encode("utf-8", errors="ignore")
    return hashlib.sha256(data).hexdigest()


def _row_value(row: Any, key: str, position: Optional[int] = None) -> Any:
    if hasattr(row, "_mapping"):
        return row._mapping.get(key)
    if isinstance(row, Mapping):
        return row.get(key)
    if position is not None and isinstance(row, (list, tuple)):
        if 0 <= position < len(row):
            return row[position]
    return None


@dataclass
class ExistingChunk:
    id: UUID
    chunk_index: int
    text_hash: str
    summary: Optional[str]
    spaces: Set[str]


@dataclass
class ExistingDocument:
    id: UUID
    version: int
    content_digest: Optional[str]
    chunks_by_index: Dict[int, ExistingChunk]
    chunks_by_hash: Dict[str, ExistingChunk]


@dataclass(frozen=True)
class TableConfig:
    """Fully qualified table names used by the ingestion persistence layer."""

    documents: str
    chunks: str
    chunk_embeddings: str
    document_embeddings: str
    entries: Optional[str]

    @property
    def mapping(self) -> Dict[str, str]:
        mapping = {
            "kb.documents": self.documents,
            "kb.chunks": self.chunks,
            "kb.chunk_embeddings": self.chunk_embeddings,
            "kb.document_embeddings": self.document_embeddings,
        }
        if self.entries:
            mapping["kb.entries"] = self.entries
        return mapping


def table_config_from_namespace(namespace: str, *, entries: Optional[str] = None) -> TableConfig:
    prefix = namespace.strip()
    if not prefix:
        prefix = "kb"

    def qualify(table: str) -> str:
        return f"{prefix}.{table}"

    if entries is None:
        entries_table: Optional[str] = "kb.entries" if prefix == "kb" else None
    else:
        entries_table = entries

    return TableConfig(
        documents=qualify("documents"),
        chunks=qualify("chunks"),
        chunk_embeddings=qualify("chunk_embeddings"),
        document_embeddings=qualify("document_embeddings"),
        entries=entries_table,
    )


DEFAULT_TABLES = table_config_from_namespace("kb")


def _apply_table_overrides(statement: str, tables: TableConfig) -> str:
    resolved = statement
    for source, target in tables.mapping.items():
        resolved = resolved.replace(source, target)
    return resolved


def _split_table_qualifier(qualified: str) -> Tuple[str, str]:
    if "." not in qualified:
        return "public", qualified
    schema, table = qualified.split(".", 1)
    return schema, table


_BASE_SCHEMA_PATCHES = [
    "ALTER TABLE kb.documents ADD COLUMN IF NOT EXISTS source_uri TEXT",
    "ALTER TABLE kb.documents ADD COLUMN IF NOT EXISTS file_name TEXT",
    "ALTER TABLE kb.documents ADD COLUMN IF NOT EXISTS summary TEXT",
    "ALTER TABLE kb.documents ADD COLUMN IF NOT EXISTS content_digest TEXT",
    "ALTER TABLE kb.documents ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE kb.documents ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()",
    "ALTER TABLE kb.chunks ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE kb.chunks ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()",
    "ALTER TABLE kb.chunk_embeddings ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE kb.document_embeddings ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE kb.entries ADD COLUMN IF NOT EXISTS document_id UUID REFERENCES kb.documents(id) ON DELETE SET NULL",
    "ALTER TABLE kb.entries ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE kb.entries ADD COLUMN IF NOT EXISTS file_name TEXT",
    "ALTER TABLE kb.entries ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()",
]


def _schema_patch_statements(tables: TableConfig) -> List[str]:
    statements: List[str] = []
    for statement in _BASE_SCHEMA_PATCHES:
        if "kb.entries" in statement and not tables.entries:
            continue
        statements.append(_apply_table_overrides(statement, tables))
    return statements


def _ensure_schema_columns(conn, tables: TableConfig) -> None:
    for statement in _schema_patch_statements(tables):
        conn.execute(text(statement))


def _load_existing_document(
    conn,
    tables: TableConfig,
    source_uri: Optional[str],
) -> Optional[ExistingDocument]:
    if not source_uri:
        return None

    _ensure_schema_columns(conn, tables)

    doc_schema, doc_table = _split_table_qualifier(tables.documents)

    def _document_columns() -> Set[str]:
        rows = conn.execute(
            text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = :schema AND table_name = :table
                """
            ),
            {"schema": doc_schema, "table": doc_table},
        ).fetchall()
        return {
            str(_row_value(row, "column_name", 0))
            for row in rows
            if _row_value(row, "column_name", 0)
        }

    available_columns = _document_columns()
    required_defs = {
        "version": "ALTER TABLE kb.documents ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1",
        "source_uri": "ALTER TABLE kb.documents ADD COLUMN IF NOT EXISTS source_uri TEXT",
        "content_digest": "ALTER TABLE kb.documents ADD COLUMN IF NOT EXISTS content_digest TEXT",
        "file_name": "ALTER TABLE kb.documents ADD COLUMN IF NOT EXISTS file_name TEXT",
        "summary": "ALTER TABLE kb.documents ADD COLUMN IF NOT EXISTS summary TEXT",
        "updated_at": "ALTER TABLE kb.documents ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()",
    }
    required_defs = {
        key: _apply_table_overrides(statement, tables)
        for key, statement in required_defs.items()
    }

    missing_columns = {name for name in ("version", "source_uri") if name not in available_columns}
    if missing_columns:
        for column_name in missing_columns:
            statement = required_defs.get(column_name)
            if statement:
                conn.execute(text(statement))
        available_columns = _document_columns()

    if "version" not in available_columns or "source_uri" not in available_columns:
        return None

    for optional_column in ("content_digest", "file_name", "summary", "updated_at"):
        if optional_column not in available_columns:
            statement = required_defs.get(optional_column)
            if statement:
                conn.execute(text(statement))
    available_columns = _document_columns()

    select_parts = ["id::text AS id", "version"]
    if "content_digest" in available_columns:
        select_parts.append("content_digest")
    else:
        select_parts.append("NULL AS content_digest")

    select_sql = _apply_table_overrides(
        """
        SELECT {columns}
        FROM kb.documents
        WHERE source_uri = :source_uri
        ORDER BY version DESC
        LIMIT 1
    """.format(columns=", ".join(select_parts)),
        tables,
    )

    doc_row = conn.execute(
        text(select_sql),
        {"source_uri": source_uri},
    ).fetchone()

    if not doc_row:
        return None

    doc_id_value = _row_value(doc_row, "id", 0)
    version_value = _row_value(doc_row, "version", 1)
    digest_value = _row_value(doc_row, "content_digest", 2)
    if not doc_id_value:
        return None

    document_id = UUID(str(doc_id_value))
    version = int(version_value or 1)
    content_digest = str(digest_value) if digest_value is not None else None

    chunk_rows = conn.execute(
        text(
            _apply_table_overrides(
                """
            SELECT id::text AS id, chunk_index, text, summary
            FROM kb.chunks
            WHERE document_id = :document_id
            """
                ,
                tables,
            )
        ),
        {"document_id": str(document_id)},
    ).fetchall()

    if not chunk_rows:
        return ExistingDocument(
            id=document_id,
            version=version,
            content_digest=content_digest,
            chunks_by_index={},
            chunks_by_hash={},
        )

    chunk_map: Dict[int, ExistingChunk] = {}
    hash_map: Dict[str, ExistingChunk] = {}
    chunk_ids: List[UUID] = []
    for row in chunk_rows:
        chunk_id_val = _row_value(row, "id", 0)
        chunk_index_val = _row_value(row, "chunk_index", 1)
        text_val = _row_value(row, "text", 2) or ""
        summary_val = _row_value(row, "summary", 3)
        if chunk_id_val is None or chunk_index_val is None:
            continue
        chunk_id = UUID(str(chunk_id_val))
        text_hash = _chunk_text_hash(str(text_val))
        chunk = ExistingChunk(
            id=chunk_id,
            chunk_index=int(chunk_index_val),
            text_hash=text_hash,
            summary=str(summary_val) if summary_val is not None else None,
            spaces=set(),
        )
        chunk_map[chunk.chunk_index] = chunk
        hash_map[text_hash] = chunk
        chunk_ids.append(chunk_id)

    chunk_by_id: Dict[UUID, ExistingChunk] = {chunk.id: chunk for chunk in chunk_map.values()}

    if chunk_ids:
        space_rows = conn.execute(
            text(
                _apply_table_overrides(
                    """
                SELECT ce.chunk_id::text AS chunk_id, es.name
                FROM kb.chunk_embeddings AS ce
                JOIN kb.embedding_spaces AS es ON es.id = ce.space_id
                WHERE ce.chunk_id = ANY(:chunk_ids)
                """
                    ,
                    tables,
                )
            ),
            {"chunk_ids": chunk_ids},
        ).fetchall()
        for row in space_rows or []:
            chunk_id_val = _row_value(row, "chunk_id", 0)
            space_name = _row_value(row, "name", 1)
            if not chunk_id_val or not space_name:
                continue
            try:
                chunk_uuid = UUID(str(chunk_id_val))
            except ValueError:
                continue
            chunk = chunk_by_id.get(chunk_uuid)
            if chunk:
                chunk.spaces.add(str(space_name))

    return ExistingDocument(
        id=document_id,
        version=version,
        content_digest=content_digest,
        chunks_by_index=chunk_map,
        chunks_by_hash=hash_map,
    )



def build_chunk_emotions(chunks: Sequence[Chunk]) -> Dict[int, List[Dict[str, object]]]:
    """Run emotion/sentiment analysis for each chunk and return a mapping."""

    results: Dict[int, List[Dict[str, object]]] = {}
    for chunk in chunks:
        sample_source = (chunk.summary or chunk.text or "").strip()
        if not sample_source:
            continue
        if len(sample_source) > EMOTION_PREVIEW_CHAR_LIMIT:
            sample_source = sample_source[:EMOTION_PREVIEW_CHAR_LIMIT]
        try:
            signals = analyze_emotions(sample_source)
        except Exception as exc:  # pragma: no cover - depends on runtime services
            LOGGER.warning(
                "Emotion analysis failed: %s",
                exc,
                extra={
                    "chunk_index": chunk.chunk_index,
                    "ingest_item": str(chunk.ingest_item_id),
                    "error": str(exc),
                },
            )
            continue
        if signals:
            results[chunk.chunk_index] = signals
    return results


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
                INSERT INTO kb.ingest_items (id, job_id, source_type, source_uri, display_name, status, metadata, is_dev)
                VALUES (:id, :job_id, :source_type, :source_uri, :display_name, :status, CAST(:metadata AS JSONB), :is_dev)
                ON CONFLICT (id) DO UPDATE
                SET source_type = EXCLUDED.source_type,
                    source_uri = EXCLUDED.source_uri,
                    display_name = EXCLUDED.display_name,
                    status = EXCLUDED.status,
                    metadata = EXCLUDED.metadata,
                    is_dev = EXCLUDED.is_dev,
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
                "is_dev": item.is_dev,
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
    digest = _content_digest(content)
    metadata["content_digest"] = digest
    item.metadata["content_digest"] = digest

    LOGGER.debug("Acquired %d bytes for ingest item %s", len(content), item.id)
    return AcquiredSource(ingest_item_id=item.id, content=content, metadata=metadata)


@task
def docling_normalize(source: AcquiredSource) -> NormalizedDocument:
    """Normalize source bytes via Docling, falling back to raw decoding on failure."""

    settings = get_settings()
    docling_tool = get_tool_config("docling")
    if not docling_tool.resolved_endpoint:
        raise RuntimeError("docling endpoint is not configured")
    base_url = docling_tool.resolved_endpoint
    timeout_seconds = float(docling_tool.timeout_s or settings.docling_timeout_seconds)
    poll_interval = settings.docling_poll_interval_seconds
    filename = str(source.metadata.get("filename") or "document")
    content_type = str(source.metadata.get("content_type") or "application/octet-stream")

    try:
        markdown, plain_text, docling_meta = _docling_convert(
            source.content,
            filename=filename,
            content_type=content_type,
            timeout=timeout_seconds,
            poll_interval=poll_interval,
            base_url=base_url,
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
        raw_html = source.content.decode("utf-8", errors="replace")
        text = _strip_html(raw_html)
        markdown = text
        metadata = {
            "language": "und",
            "docling_error": str(exc),
            "source": dict(source.metadata),
        }

    if "<" in text and ">" in text:
        stripped = _strip_html(text)
        if stripped:
            text = stripped

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

    try:
        summary = summarize_with_gemini(document.text, max_length=500)
    except Exception as exc:  # pragma: no cover - depends on external LLM
        LOGGER.error("Gemini document summary failed for %s: %s", item.id, exc)
        summary = document.text[:500].strip()
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
            metadata={"source_type": item.source_type},
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
    try:
        summaries = summarize_chunks_with_gemini(
            [chunk.text for chunk in chunks],
            max_length=256,
        )
    except Exception as exc:  # pragma: no cover - depends on external LLM
        LOGGER.warning(
            "Chunk summarization failed for %s; falling back to truncation: %s",
            item.id,
            exc,
        )
        summaries = [
            (chunk.text[:256].strip() or chunk.text[:128].strip() or "")
            for chunk in chunks
        ]
    for chunk, summary in zip(chunks, summaries):
        chunk.summary = summary
        updated.chunk_summaries[chunk.chunk_index] = summary
    LOGGER.debug("Generated %d chunk summary entries for %s", len(chunks), item.id)
    return updated


@task
def detect_emotions(item: IngestItem, chunks: List[Chunk]) -> Dict[int, List[Dict[str, object]]]:
    """Classify emotions/sentiment for each chunk."""

    signals = build_chunk_emotions(chunks)
    LOGGER.debug(
        "Detected emotions for %d/%d chunk(s) in %s",
        len(signals),
        len(chunks),
        item.id,
    )
    return signals


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
    runtime_config = get_runtime_config()

    def sql(statement: str):
        return text(_apply_table_overrides(statement, tables))
    general_model = runtime_config.active("active_emb_general")
    legal_model = runtime_config.active("active_emb_legal")
    code_model = runtime_config.active("active_emb_code")
    domain_key = (item.domain or "general").lower()
    expected_spaces: Set[str] = {general_model.name}
    if domain_key == "legal":
        expected_spaces.add(legal_model.name)
    elif domain_key == "code":
        expected_spaces.add(code_model.name)
    legal_model = runtime_config.active("active_emb_legal")
    code_model = runtime_config.active("active_emb_code")
    try:
        general_vectors = embed_with_general(texts)
    except Exception as exc:  # pragma: no cover - external dependency
        LOGGER.error(
            "General embedding failed for %s; skipping vectors: %s",
            item.id,
            exc,
        )
        general_vectors = [None] * len(chunks)
    for vector, chunk in zip(general_vectors, chunks):
        if vector is None:
            continue
        embeddings.append(
            ChunkEmbedding(
                chunk_id=chunk.id,
                space=general_model.name,
                model=general_model.identifier,
                vector=vector,
            )
        )

    domain_key = (item.domain or "general").lower()
    if domain_key == "legal":
        try:
            legal_vectors = embed_with_legal(texts)
        except Exception as exc:  # pragma: no cover - external dependency
            LOGGER.error(
                "Legal embedding failed for %s; skipping vectors: %s",
                item.id,
                exc,
            )
            legal_vectors = [None] * len(chunks)
        for vector, chunk in zip(legal_vectors, chunks):
            if vector is None:
                continue
            embeddings.append(
                ChunkEmbedding(
                    chunk_id=chunk.id,
                    space=legal_model.name,
                    model=legal_model.identifier,
                    vector=vector,
                )
            )
    elif domain_key == "code":
        try:
            code_vectors = embed_with_code(texts)
        except Exception as exc:  # pragma: no cover - external dependency
            LOGGER.error(
                "Code embedding failed for %s; skipping vectors: %s",
                item.id,
                exc,
            )
            code_vectors = [None] * len(chunks)
        for vector, chunk in zip(code_vectors, chunks):
            if vector is None:
                continue
            embeddings.append(
                ChunkEmbedding(
                    chunk_id=chunk.id,
                    space=code_model.name,
                    model=code_model.identifier,
                    vector=vector,
                )
            )
    LOGGER.info(
        "ingest chunk embeddings prepared",
        extra={
            "ingest_item_id": str(item.id),
            "spaces_used": sorted({embedding.space for embedding in embeddings}),
        },
    )
    return embeddings


@task

def persist_results(
    item: IngestItem,
    document: NormalizedDocument,
    chunks: List[Chunk],
    embeddings: List[ChunkEmbedding],
    chunk_emotions: Dict[int, List[Dict[str, object]]],
    abstractions: List[str],
    table_config: Optional[TableConfig] = None,
) -> FlowReport:
    """Persist the enriched document, chunks, and embeddings into ParadeDB."""

    engine = get_engine()
    tables = table_config or DEFAULT_TABLES
    runtime_config = get_runtime_config()

    def sql(statement: str):
        return text(_apply_table_overrides(statement, tables))

    general_model = runtime_config.active("active_emb_general")
    legal_model = runtime_config.active("active_emb_legal")
    code_model = runtime_config.active("active_emb_code")
    domain_key = (item.domain or "general").lower()
    expected_spaces: Set[str] = {general_model.name}
    if domain_key == "legal":
        expected_spaces.add(legal_model.name)
    elif domain_key == "code":
        expected_spaces.add(code_model.name)

    source_meta = document.metadata.get("source", {}) if isinstance(document.metadata, dict) else {}
    docling_meta = document.metadata.get("docling") if isinstance(document.metadata, dict) else None
    language = str(document.metadata.get("language", "und")) if isinstance(document.metadata, dict) else "und"
    tsvector_config = _tsvector_config(language)
    filename = source_meta.get("filename") or item.display_name
    content_digest = source_meta.get("content_digest")
    document_summary = item.document_summary
    if not document_summary and isinstance(document.metadata, dict):
        docling_doc = document.metadata.get("docling") or {}
        if isinstance(docling_doc, dict):
            document_summary = docling_doc.get("summary")
    if not document_summary:
        document_summary = (document.text or "").strip()[:5000]

    chunk_emotions = dict(chunk_emotions or {})

    raw_metadata: Mapping[str, Any] = item.metadata if isinstance(item.metadata, Mapping) else {}
    resolved_flags = flag_registry.resolve_flags(raw_metadata)
    is_note_flag = resolved_flags.get("is_note", False)
    is_document_flag = resolved_flags.get("is_document", not is_note_flag)
    resolved_flags["is_note"] = is_note_flag
    resolved_flags["is_document"] = is_document_flag
    active_flags = flag_registry.active_flag_names(resolved_flags)
    references = reference_utils.extract_reference_ids(raw_metadata)

    ingest_metadata: Dict[str, Any] = dict(raw_metadata)
    ingest_metadata.update(
        {
            "source": source_meta,
            "chunk_abstractions": abstractions,
            "chunk_summaries": item.chunk_summaries,
        }
    )
    ingest_metadata["is_dev"] = item.is_dev
    ingest_metadata["flags"] = dict(resolved_flags)
    ingest_metadata["active_flags"] = list(active_flags)
    ingest_metadata["references"] = references
    ingest_metadata["is_note"] = is_note_flag

    if isinstance(document.metadata, Mapping):
        doc_metadata_block: Dict[str, Any] = dict(document.metadata)
    else:
        doc_metadata_block = {}
    doc_metadata_block.setdefault("docling", docling_meta)
    doc_metadata_block["source"] = source_meta
    doc_metadata_block["flags"] = dict(resolved_flags)
    doc_metadata_block["active_flags"] = list(active_flags)
    doc_metadata_block["references"] = references
    for key in ("workspace", "document_kind", "tags", "labels"):
        if key in raw_metadata and key not in doc_metadata_block:
            doc_metadata_block[key] = raw_metadata[key]
    ingest_metadata["document_metadata"] = doc_metadata_block

    embedding_spaces: Dict[str, int] = {}
    entry_ids: List[str] = []
    id_remap: Dict[UUID, UUID] = {}
    copied_chunk_ids: List[str] = []
    new_chunk_ids: List[str] = []
    digest_matches = False
    document_version = 1
    document_id: Optional[str] = None
    task_sync_info: Optional[Dict[str, object]] = None

    with engine.begin() as conn:
        existing_document = _load_existing_document(conn, tables, item.source_uri)
        digest_matches = bool(
            existing_document
            and content_digest
            and existing_document.content_digest
            and existing_document.content_digest == content_digest
        )

        if digest_matches and existing_document:
            document_uuid = existing_document.id
            document_version = existing_document.version
        else:
            document_version = (existing_document.version + 1) if existing_document else 1
            document_uuid = uuid4()
        document_id = str(document_uuid)

        doc_metadata_payload = json.dumps(doc_metadata_block)

        if digest_matches:
            conn.execute(
                sql(
                    """
                    UPDATE kb.documents
                    SET ingest_item_id = :ingest_item_id,
                        title = :title,
                        text_full = :text_full,
                        tsv = to_tsvector(:ts_config, :text_full),
                        metadata = CAST(:metadata AS JSONB),
                        summary = :summary,
                        file_name = :file_name,
                        content_digest = :content_digest,
                        is_dev = :is_dev,
                        updated_at = NOW()
                    WHERE id = :id
                    """
                ),
                {
                    "id": document_id,
                    "ingest_item_id": str(item.id),
                    "title": document.metadata.get("docling", {}).get("title") if isinstance(document.metadata, dict) else item.display_name,
                    "text_full": document.text,
                    "ts_config": tsvector_config,
                    "metadata": doc_metadata_payload,
                    "summary": document_summary,
                    "file_name": filename,
                    "content_digest": content_digest,
                    "is_dev": item.is_dev,
                },
            )
        else:
            conn.execute(
                sql(
                    """
                    INSERT INTO kb.documents (
                        id,
                        ingest_item_id,
                        source_uri,
                        file_name,
                        title,
                        text_full,
                        tsv,
                        metadata,
                        summary,
                        content_digest,
                        version,
                        is_dev
                    )
                    VALUES (
                        :id,
                        :ingest_item_id,
                        :source_uri,
                        :file_name,
                        :title,
                        :text_full,
                        to_tsvector(:ts_config, :text_full),
                        CAST(:metadata AS JSONB),
                        :summary,
                        :content_digest,
                        :version,
                        :is_dev
                    )
                    ON CONFLICT (id) DO UPDATE
                    SET ingest_item_id = EXCLUDED.ingest_item_id,
                        source_uri = EXCLUDED.source_uri,
                        file_name = EXCLUDED.file_name,
                        title = EXCLUDED.title,
                        text_full = EXCLUDED.text_full,
                        tsv = EXCLUDED.tsv,
                        metadata = EXCLUDED.metadata,
                        summary = EXCLUDED.summary,
                        content_digest = EXCLUDED.content_digest,
                        version = EXCLUDED.version,
                        is_dev = EXCLUDED.is_dev,
                        updated_at = NOW()
                    """
                ),
                {
                    "id": document_id,
                    "ingest_item_id": str(item.id),
                    "source_uri": item.source_uri,
                    "file_name": filename,
                    "title": document.metadata.get("docling", {}).get("title") if isinstance(document.metadata, dict) else item.display_name,
                    "text_full": document.text,
                    "ts_config": tsvector_config,
                    "metadata": doc_metadata_payload,
                    "summary": document_summary,
                    "content_digest": content_digest,
                    "version": document_version,
                    "is_dev": item.is_dev,
                },
            )

        ingest_metadata["document_id"] = document_id
        ingest_metadata["document_version"] = document_version
        ingest_metadata["digest_match"] = digest_matches
        ingest_metadata["filename"] = filename
        ingest_metadata["is_note"] = is_note_flag

        if item.is_dev:
            conn.execute(
                text(
                    """
                    INSERT INTO kb.dev_documents_meta (
                        document_id,
                        source_uri,
                        file_name,
                        ingest_item_id,
                        domain,
                        domain_confidence,
                        classifier_source,
                        extra
                    )
                    VALUES (
                        :document_id,
                        :source_uri,
                        :file_name,
                        :ingest_item_id,
                        :domain,
                        :domain_confidence,
                        :classifier_source,
                        CAST(:extra AS JSONB)
                    )
                    ON CONFLICT (document_id) DO UPDATE
                    SET source_uri = EXCLUDED.source_uri,
                        file_name = EXCLUDED.file_name,
                        ingest_item_id = EXCLUDED.ingest_item_id,
                        domain = EXCLUDED.domain,
                        domain_confidence = EXCLUDED.domain_confidence,
                        classifier_source = EXCLUDED.classifier_source,
                        extra = EXCLUDED.extra,
                        updated_at = NOW()
                    """
                ),
                {
                    "document_id": document_id,
                    "source_uri": item.source_uri,
                    "file_name": filename,
                    "ingest_item_id": str(item.id),
                    "domain": item.domain,
                    "domain_confidence": item.domain_confidence,
                    "classifier_source": (
                        (item.metadata or {}).get("classification", {}).get("source")
                        if isinstance(item.metadata, dict)
                        else None
                    ),
                    "extra": json.dumps(
                        {
                            "ingest_metadata": ingest_metadata,
                            "classification": item.metadata.get("classification"),
                        }
                    ),
                },
            )

        markdown_source = getattr(document, "markdown", None) or document.text
        if markdown_source:
            parsed_tasks_doc = task_registry.sync_task_document(
                conn,
                document_id=document_id,
                ingest_item_id=str(item.id),
                markdown=markdown_source,
                is_dev=item.is_dev,
            )
            if parsed_tasks_doc:
                task_sync_info = {
                    "project_key": parsed_tasks_doc.project.project_key,
                    "task_count": len(parsed_tasks_doc.tasks),
                }
                ingest_metadata["task_sync"] = task_sync_info

        existing_by_index = existing_document.chunks_by_index if existing_document else {}
        existing_by_hash = existing_document.chunks_by_hash if existing_document else {}

        chunk_records: List[Dict[str, object]] = []
        copy_pairs: List[Tuple[ExistingChunk, Chunk]] = []

        for chunk in chunks:
            original_id = chunk.id
            chunk_hash = _chunk_text_hash(chunk.text)
            existing_chunk: Optional[ExistingChunk] = None
            if existing_document:
                candidate = existing_by_index.get(chunk.chunk_index)
                if candidate and candidate.text_hash == chunk_hash:
                    existing_chunk = candidate
                else:
                    candidate = existing_by_hash.get(chunk_hash)
                    if candidate and candidate.text_hash == chunk_hash:
                        existing_chunk = candidate

            if digest_matches and existing_chunk:
                if original_id != existing_chunk.id:
                    id_remap[original_id] = existing_chunk.id
                    chunk.id = existing_chunk.id
                continue

            if (not digest_matches) and existing_chunk:
                new_id = uuid4()
                id_remap[original_id] = new_id
                chunk.id = new_id
                copy_pairs.append((existing_chunk, chunk))
                copied_chunk_ids.append(str(new_id))
                continue

            if original_id != chunk.id:
                id_remap[original_id] = chunk.id

            metadata_payload: Dict[str, object] = {"source_type": item.source_type}
            if getattr(chunk, "metadata", None):
                try:
                    metadata_payload.update(dict(chunk.metadata))
                except Exception:
                    metadata_payload["chunk_metadata_error"] = "unserializable_metadata"
            metadata_payload.setdefault("flags", dict(resolved_flags))
            metadata_payload.setdefault("active_flags", list(active_flags))
            if references:
                metadata_payload.setdefault("references", references)
            mapping: Dict[str, object] = {
                "id": str(chunk.id),
                "ingest_item_id": str(item.id),
                "document_id": document_id,
                "chunk_index": chunk.chunk_index,
                "heading_path": chunk.heading_path,
                "kind": chunk.kind,
                "text": chunk.text,
                "summary": chunk.summary,
                "token_count": chunk.token_count,
                "overlap_tokens": chunk.overlap_tokens,
                "ner_entities": json.dumps(chunk.ner_entities),
                "metadata": json.dumps(metadata_payload),
                "is_dev": item.is_dev,
            }
            chunk_records.append({"chunk": chunk, "mapping": mapping, "copied": False})
            new_chunk_ids.append(mapping["id"])

        if id_remap:
            for embedding in embeddings:
                remapped = id_remap.get(embedding.chunk_id)
                if remapped is not None:
                    embedding.chunk_id = remapped

        embeddings_by_chunk: Dict[str, Dict[str, List[float]]] = {}
        for embedding in embeddings:
            embeddings_by_chunk.setdefault(str(embedding.chunk_id), {})[embedding.space] = embedding.vector

        if copy_pairs:
            source_ids = [str(source.id) for source, _ in copy_pairs]
            chunk_rows = conn.execute(
                sql(
                    """
                    SELECT id::text AS id,
                           heading_path,
                           kind,
                           text,
                           summary,
                           token_count,
                           overlap_tokens,
                           ner_entities,
                           metadata
                    FROM kb.chunks
                    WHERE id = ANY(:ids)
                    """
                ),
                {"ids": source_ids},
            ).mappings()
            chunk_row_map = {row["id"]: row for row in chunk_rows}

            embedding_rows = conn.execute(
                sql(
                    """
                    SELECT ce.chunk_id::text AS chunk_id,
                           es.name,
                           ce.embedding
                    FROM kb.chunk_embeddings AS ce
                    JOIN kb.embedding_spaces AS es ON es.id = ce.space_id
                    WHERE ce.chunk_id = ANY(:ids)
                    """
                ),
                {"ids": source_ids},
            ).mappings()
            embedding_map: Dict[str, Dict[str, List[float]]] = {}
            for row in embedding_rows:
                chunk_id_value = row["chunk_id"]
                vector_value = row["embedding"]
                if hasattr(vector_value, "tolist"):
                    vector_payload = list(vector_value.tolist())
                elif isinstance(vector_value, (list, tuple)):
                    vector_payload = list(vector_value)
                else:
                    try:
                        vector_payload = list(vector_value)
                    except TypeError:
                        try:
                            vector_payload = json.loads(vector_value)
                        except (TypeError, json.JSONDecodeError):
                            continue
                embedding_map.setdefault(chunk_id_value, {})[row["name"]] = vector_payload

            for source_chunk, target_chunk in copy_pairs:
                row = chunk_row_map.get(str(source_chunk.id))
                if row is None:
                    continue
                metadata_payload = row.get("metadata") or {}
                if isinstance(metadata_payload, str):
                    try:
                        metadata_payload = json.loads(metadata_payload)
                    except json.JSONDecodeError:
                        metadata_payload = {}
                if not isinstance(metadata_payload, dict):
                    metadata_payload = {}
                metadata_payload.setdefault("source_type", item.source_type)
                if getattr(target_chunk, "metadata", None):
                    try:
                        metadata_payload.update(dict(target_chunk.metadata))
                    except Exception:
                        metadata_payload["chunk_metadata_error"] = "unserializable_metadata"
                metadata_payload.setdefault("flags", dict(resolved_flags))
                metadata_payload.setdefault("active_flags", list(active_flags))
                if references:
                    metadata_payload.setdefault("references", references)
                mapping: Dict[str, object] = {
                    "id": str(target_chunk.id),
                    "ingest_item_id": str(item.id),
                    "document_id": document_id,
                    "chunk_index": target_chunk.chunk_index,
                    "heading_path": row.get("heading_path") or target_chunk.heading_path,
                    "kind": row.get("kind") or target_chunk.kind,
                    "text": row.get("text") or target_chunk.text,
                    "summary": row.get("summary") or target_chunk.summary,
                    "token_count": row.get("token_count") or target_chunk.token_count,
                    "overlap_tokens": row.get("overlap_tokens") or target_chunk.overlap_tokens,
                    "ner_entities": json.dumps(row.get("ner_entities") or target_chunk.ner_entities or []),
                    "metadata": json.dumps(metadata_payload),
                    "is_dev": item.is_dev,
                }
                vectors = embedding_map.get(str(source_chunk.id)) or {}
                if vectors:
                    embeddings_by_chunk[str(target_chunk.id)] = vectors
                chunk_records.append(
                    {
                        "chunk": target_chunk,
                        "mapping": mapping,
                        "copied": True,
                        "copied_from": str(source_chunk.id),
                        "copied_vectors": vectors,
                    }
                )
                new_chunk_ids.append(mapping["id"])

        entry_meta_base: Dict[str, object] = {
            "document_id": document_id,
            "document_version": document_version,
            "filename": filename,
            "domain": item.domain,
            "language": language,
            "source": source_meta,
            "document_metadata": doc_metadata_block,
            "ingest_metadata": ingest_metadata,
            "chunk_summaries": item.chunk_summaries,
            "abstractions": abstractions,
            "is_dev": item.is_dev,
            "is_note": is_note_flag,
            "is_document": is_document_flag,
            "flags": dict(resolved_flags),
            "active_flags": list(active_flags),
            "references": references,
        }

        persisted_chunk_ids: Set[str] = set()

        for record in chunk_records:
            chunk = record["chunk"]
            mapping = record["mapping"]
            copied_from = record.get("copied_from")
            vectors = record.get("copied_vectors") or embeddings_by_chunk.get(mapping["id"], {})

            conn.execute(
                sql(
                    """
                    DELETE FROM kb.chunks
                    WHERE ingest_item_id = :ingest_item_id
                      AND chunk_index = :chunk_index
                      AND id <> :id
                    """
                ),
                {
                    "ingest_item_id": str(item.id),
                    "chunk_index": chunk.chunk_index,
                    "id": mapping["id"],
                },
            )

            conn.execute(
                sql(
                    """
                    INSERT INTO kb.chunks (
                        id,
                        ingest_item_id,
                        document_id,
                        chunk_index,
                        heading_path,
                        kind,
                        text,
                        summary,
                        token_count,
                        overlap_tokens,
                        ner_entities,
                        tsv,
                        metadata,
                        version,
                        is_dev
                    )
                    VALUES (
                        :id,
                        :ingest_item_id,
                        :document_id,
                        :chunk_index,
                        :heading_path,
                        :kind,
                        :text,
                        :summary,
                        :token_count,
                        :overlap_tokens,
                        CAST(:ner_entities AS JSONB),
                        to_tsvector(:ts_config, :text),
                        CAST(:metadata AS JSONB),
                        :version,
                        :is_dev
                    )
                    ON CONFLICT (id) DO UPDATE
                    SET text = EXCLUDED.text,
                        summary = EXCLUDED.summary,
                        token_count = EXCLUDED.token_count,
                        overlap_tokens = EXCLUDED.overlap_tokens,
                        ner_entities = EXCLUDED.ner_entities,
                        tsv = EXCLUDED.tsv,
                        metadata = EXCLUDED.metadata,
                        version = EXCLUDED.version,
                        is_dev = EXCLUDED.is_dev,
                        updated_at = NOW()
                    """
                ),
                {
                    **mapping,
                    "ts_config": tsvector_config,
                    "version": document_version,
                },
            )

            persisted_chunk_ids.add(mapping["id"])

            chunk_vectors = dict(vectors or {})
            for space_name, vector in chunk_vectors.items():
                try:
                    model_cfg = runtime_config.model(space_name)
                except RuntimeError:
                    LOGGER.warning("Skipping unknown embedding space", extra={"space": space_name})
                    continue
                space_id = embedding_spaces.get(model_cfg.name)
                if space_id is None:
                    expected_dims = model_cfg.require_dims()
                    if isinstance(vector, (list, tuple)) and len(vector) != expected_dims:
                        LOGGER.warning(
                            "Embedding dimensionality mismatch",
                            extra={
                                "space": space_name,
                                "expected": expected_dims,
                                "actual": len(vector),
                            },
                        )
                    space_id = _ensure_embedding_space(conn, model_cfg)
                    _ensure_hnsw_index(conn, tables, space_id, model_cfg.name)
                    embedding_spaces[model_cfg.name] = space_id
                else:
                    embedding_spaces.setdefault(model_cfg.name, space_id)

                if hasattr(vector, "tolist"):
                    vector_literal = _vector_literal(vector.tolist())
                elif isinstance(vector, (list, tuple)):
                    vector_literal = _vector_literal(list(vector))
                else:
                    vector_literal = vector

                conn.execute(
                    sql(
                        """
                        INSERT INTO kb.chunk_embeddings (chunk_id, space_id, embedding, version)
                        VALUES (:chunk_id, :space_id, CAST(:embedding AS vector), :version)
                        ON CONFLICT (chunk_id, space_id) DO UPDATE
                        SET embedding = EXCLUDED.embedding,
                            version = EXCLUDED.version,
                            created_at = NOW()
                        """
                    ),
                    {
                        "chunk_id": mapping["id"],
                        "space_id": space_id,
                        "embedding": vector_literal,
                        "version": document_version,
                    },
                )

            entry_meta = dict(entry_meta_base)
            entry_meta.update(
                {
                    "chunk_index": chunk.chunk_index,
                    "heading_path": chunk.heading_path,
                    "kind": chunk.kind,
                    "token_count": chunk.token_count,
                    "overlap_tokens": chunk.overlap_tokens,
                }
            )
            if copied_from:
                entry_meta["copied_from_chunk_id"] = copied_from
            emotions_payload = chunk_emotions.get(chunk.chunk_index)
            if emotions_payload:
                entry_meta["emotions"] = emotions_payload
            additional_vectors = {
                name: vector
                for name, vector in (chunk_vectors or {}).items()
                if name != general_model.name
            }
            if additional_vectors:
                entry_meta["additional_embeddings"] = additional_vectors

            needs_metadata = bool(
                isinstance(document.metadata, dict)
                and document.metadata.get("docling_error")
            )
            meta_flags: Set[str] = set()
            existing_flags = entry_meta.get("meta_flags")
            if existing_flags:
                for flag in _iter_flags(existing_flags):
                    if flag:
                        meta_flags.add(str(flag))
            if needs_metadata:
                meta_flags.add("needs_metadata")
            if meta_flags:
                entry_meta["meta_flags"] = sorted(meta_flags)
            elif "meta_flags" in entry_meta:
                entry_meta.pop("meta_flags", None)

            is_chat = bool(item.source_type and item.source_type.lower() == "chat")
            general_vector = chunk_vectors.get(general_model.name)
            if tables.entries:
                conn.execute(
                    sql(
                        """
                        INSERT INTO kb.entries (
                            id,
                        session_id,
                        ingest_item_id,
                        source,
                        uri,
                        title,
                        author,
                        content,
                        summary,
                        meta,
                        ner,
                        emotions,
                        embedding,
                        document_id,
                        version,
                        file_name,
                        is_dev,
                        is_document,
                        is_note,
                        needs_metadata,
                        is_chat
                    )
                    VALUES (
                        :id,
                        :session_id,
                        :ingest_item_id,
                        :source,
                        :uri,
                        :title,
                        :author,
                        :content,
                        :summary,
                        CAST(:meta AS JSONB),
                        CAST(:ner AS JSONB),
                        CAST(:emotions AS JSONB),
                        CAST(:embedding AS vector),
                        :document_id,
                        :version,
                        :file_name,
                        :is_dev,
                        :is_document,
                        :is_note,
                        :needs_metadata,
                        :is_chat
                    )
                    ON CONFLICT (id) DO UPDATE
                    SET content = EXCLUDED.content,
                        summary = EXCLUDED.summary,
                        meta = EXCLUDED.meta,
                        ner = EXCLUDED.ner,
                        emotions = EXCLUDED.emotions,
                        embedding = EXCLUDED.embedding,
                        document_id = EXCLUDED.document_id,
                        version = EXCLUDED.version,
                        file_name = EXCLUDED.file_name,
                        is_dev = EXCLUDED.is_dev,
                        is_document = EXCLUDED.is_document,
                        is_note = EXCLUDED.is_note,
                        needs_metadata = EXCLUDED.needs_metadata,
                        is_chat = EXCLUDED.is_chat,
                        updated_at = NOW()
                    """
                    ),
                    {
                        "id": mapping["id"],
                        "session_id": None,
                        "ingest_item_id": str(item.id),
                        "source": item.source_type,
                        "uri": item.source_uri,
                        "title": document.metadata.get("docling", {}).get("title") if isinstance(document.metadata, dict) else item.display_name,
                        "author": document.metadata.get("docling", {}).get("author") if isinstance(document.metadata, dict) else None,
                        "content": mapping["text"],
                        "summary": mapping["summary"],
                        "meta": json.dumps(entry_meta),
                        "ner": mapping["ner_entities"],
                        "emotions": json.dumps(emotions_payload or []),
                        "embedding": _vector_literal(general_vector) if isinstance(general_vector, (list, tuple)) else (general_vector if general_vector is not None else None),
                        "document_id": document_id,
                        "version": document_version,
                        "file_name": filename,
                        "is_dev": item.is_dev,
                        "is_document": is_document_flag,
                        "is_note": is_note_flag,
                        "needs_metadata": needs_metadata,
                        "is_chat": is_chat,
                    },
                )
                if meta_flags:
                    _trigger_metadata_flag_flows(
                        entry_id=mapping["id"],
                        ingest_item_id=str(item.id),
                        flags=sorted(meta_flags),
                        context={
                            "source_uri": item.source_uri,
                            "file_name": filename,
                            "needs_metadata": needs_metadata,
                        },
                    )
                entry_ids.append(mapping["id"])

        if digest_matches:
            for embedding_payload in embeddings:
                if str(embedding_payload.chunk_id) in persisted_chunk_ids:
                    continue
                try:
                    model_cfg = runtime_config.model(embedding_payload.space)
                except RuntimeError:
                    continue
                space_id = embedding_spaces.get(model_cfg.name)
                if space_id is None:
                    expected_dims = model_cfg.require_dims()
                    actual_dims = len(embedding_payload.vector)
                    if actual_dims != expected_dims:
                        LOGGER.warning(
                            "Embedding dimensionality mismatch",
                            extra={
                                "space": embedding_payload.space,
                                "expected": expected_dims,
                                "actual": actual_dims,
                            },
                        )
                    space_id = _ensure_embedding_space(conn, model_cfg)
                    _ensure_hnsw_index(conn, tables, space_id, model_cfg.name)
                    embedding_spaces[model_cfg.name] = space_id
                else:
                    embedding_spaces.setdefault(model_cfg.name, space_id)

                vector_literal = _vector_literal(embedding_payload.vector)
                conn.execute(
                    sql(
                        """
                        INSERT INTO kb.chunk_embeddings (chunk_id, space_id, embedding, version)
                        VALUES (:chunk_id, :space_id, CAST(:embedding AS vector), :version)
                        ON CONFLICT (chunk_id, space_id) DO UPDATE
                        SET embedding = EXCLUDED.embedding,
                            version = EXCLUDED.version,
                            created_at = NOW()
                        """
                    ),
                    {
                        "chunk_id": str(embedding_payload.chunk_id),
                        "space_id": space_id,
                        "embedding": vector_literal,
                        "version": document_version,
                    },
                )

        document_vector: Optional[List[float]] = None
        if not digest_matches:
            doc_text_for_embedding = (document.text or "").strip()
            if doc_text_for_embedding:
                try:
                    truncated = doc_text_for_embedding[:GENERAL_EMBED_CHAR_LIMIT]
                    doc_embedding_result = embed_with_general([truncated])
                    if doc_embedding_result:
                        document_vector = list(doc_embedding_result[0])
                except Exception as exc:  # pragma: no cover
                    LOGGER.warning(
                        "Document embedding failed",
                        extra={"ingest_item": str(item.id), "error": str(exc)},
                    )

        if document_vector:
            general_space_id = embedding_spaces.get(general_model.name)
            if general_space_id is None:
                general_space_id = _ensure_embedding_space(conn, general_model)
                _ensure_hnsw_index(conn, tables, general_space_id, general_model.name)
                embedding_spaces[general_model.name] = general_space_id
            conn.execute(
                sql(
                    """
                    INSERT INTO kb.document_embeddings (document_id, space_id, embedding, version)
                    VALUES (:document_id, :space_id, CAST(:embedding AS vector), :version)
                    ON CONFLICT (document_id, space_id) DO UPDATE
                    SET embedding = EXCLUDED.embedding,
                        version = EXCLUDED.version,
                        created_at = NOW()
                    """
                ),
                {
                    "document_id": document_id,
                    "space_id": general_space_id,
                    "embedding": _vector_literal(document_vector),
                    "version": document_version,
                },
            )

        conn.execute(
            text(
                """
                UPDATE kb.ingest_items
                SET metadata = CAST(:metadata AS JSONB),
                    document_summary = :document_summary,
                    domain = :domain,
                    domain_confidence = :domain_confidence,
                    is_dev = :is_dev,
                    updated_at = NOW()
                WHERE id = :id
                """
            ),
            {
                "metadata": json.dumps(ingest_metadata),
                "document_summary": document_summary,
                "domain": item.domain,
                "domain_confidence": item.domain_confidence,
                "is_dev": item.is_dev,
                "id": str(item.id),
            },
        )

    ner_total = sum(len(chunk.ner_entities) for chunk in chunks)
    inserted_chunk_count = len(new_chunk_ids)

    report_metadata: Dict[str, object] = {
        "document_id": document_id,
        "document_version": document_version,
        "digest_match": digest_matches,
        "copied_chunk_ids": copied_chunk_ids,
        "new_chunk_ids": new_chunk_ids,
        "entry_ids": entry_ids,
        "expected_spaces": sorted(expected_spaces),
        "filename": filename,
    }
    if task_sync_info:
        report_metadata["task_sync"] = task_sync_info

    item.metadata = ingest_metadata

    embedding_space_names = sorted(embedding_spaces.keys())

    report = FlowReport(
        ingest_item=item.copy(),
        chunk_count=inserted_chunk_count,
        embedding_spaces=embedding_space_names,
        job_id=str(item.job_id) if item.job_id else None,
        metadata=report_metadata,
    )
    LOGGER.debug(
        "Persisted ingest item %s to knowledge base (version %s)", item.id, document_version
    )
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


def _content_digest(content: bytes) -> str:
    """Compute a reproducible digest for raw document content."""

    return hashlib.sha256(content).hexdigest()


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


class _HTMLStripper(HTMLParser):
    """Utility that removes HTML markup while preserving readable text."""

    _BLOCK_TAGS = {
        "p",
        "br",
        "div",
        "li",
        "section",
        "article",
        "header",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }

    def __init__(self) -> None:
        super().__init__()
        self._chunks: List[str] = []
        self._suppress_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style"}:
            self._suppress_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._suppress_depth:
            self._suppress_depth -= 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._suppress_depth:
            return
        text = data.strip()
        if text:
            self._chunks.append(text)

    def get_text(self) -> str:
        combined = " ".join(self._chunks)
        return re.sub(r"\s+", " ", combined).strip()


def _strip_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    stripper = _HTMLStripper()
    try:
        stripper.feed(raw_html)
    except Exception:  # pragma: no cover - defensive against malformed markup
        return re.sub(r"<[^>]+>", " ", raw_html)
    text = stripper.get_text()
    if not text:
        return re.sub(r"<[^>]+>", " ", raw_html)
    return text



def _ensure_embedding_space(conn, model_cfg: ModelConfig) -> int:
    existing = conn.execute(
        text("SELECT id FROM kb.embedding_spaces WHERE name = :name"),
        {"name": model_cfg.name},
    ).fetchone()
    if existing:
        return existing[0]

    result = conn.execute(
        text(
            """
            INSERT INTO kb.embedding_spaces (name, model, provider, dims, distance_metric)
            VALUES (:name, :model, :provider, :dims, :distance_metric)
            RETURNING id
            """
        ),
        {
            "name": model_cfg.name,
            "model": model_cfg.identifier,
            "provider": model_cfg.provider,
            "dims": model_cfg.require_dims(),
            "distance_metric": model_cfg.config.get("distance_metric", "cosine"),
        },
    )
    return result.scalar_one()


def _ensure_hnsw_index(conn, tables: TableConfig, space_id: int, space_name: str) -> None:
    index_safe = re.sub(r"[^a-z0-9_]+", "_", space_name.lower())
    table_safe = re.sub(r"[^a-z0-9_]+", "_", tables.chunk_embeddings.replace(".", "_"))
    chunk_index = f"idx_{table_safe}_{index_safe}"
    safe_space_id = int(space_id)
    chunk_stmt = text(
        f"""
        CREATE INDEX IF NOT EXISTS {chunk_index}
        ON {tables.chunk_embeddings} USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 200)
        WHERE space_id = {safe_space_id}
        """
    )
    conn.execute(chunk_stmt)
    doc_table_safe = re.sub(r"[^a-z0-9_]+", "_", tables.document_embeddings.replace(".", "_"))
    document_index = f"idx_{doc_table_safe}_{index_safe}"
    document_stmt = text(
        f"""
        CREATE INDEX IF NOT EXISTS {document_index}
        ON {tables.document_embeddings} USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 200)
        WHERE space_id = {safe_space_id}
        """
    )
    conn.execute(document_stmt)


def _vector_literal(values: Sequence[float]) -> str:
    return "[" + ",".join(f"{value:.10f}" for value in values) + "]"


def _tsvector_config(language: str) -> str:
    if language.lower().startswith("en"):
        return "english"
    return "simple"
