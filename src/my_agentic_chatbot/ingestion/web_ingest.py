"""Helpers for ingesting curated web captures into the knowledge base."""

from __future__ import annotations

import json
import logging
from typing import Dict, Iterable, List, Optional, Tuple
from uuid import UUID, uuid4

from sqlalchemy import text

from etl.tasks import chunker
from etl.tasks.intake_models import Chunk, ChunkEmbedding, IngestItem, NormalizedDocument, new_ingest_item
from etl.tasks.intake_tasks import (
    CHUNK_TOKENS_DEFAULT,
    OVERLAP_MAX_PCT_DEFAULT,
    build_chunk_emotions,
    persist_results,
)
from etl.tasks.model_clients import (
    embed_with_code,
    embed_with_general,
    embed_with_legal,
    summarize_chunks_with_gemini,
    summarize_with_gemini,
)

from ..storage.connection import get_engine
from ..runtime_config import get_runtime_config

LOGGER = logging.getLogger(__name__)


def ingest_web_capture(
    *,
    run_id: str,
    requirement_id: Optional[str],
    url: str,
    display_name: str,
    markdown: str,
    plain_text: str,
    curated_summary: Optional[str] = None,
    metadata: Optional[Dict[str, object]] = None,
    domain: str = "web",
) -> Tuple[UUID, List[UUID]]:
    """Persist a curated web document into the KB, returning doc and chunk ids."""

    item_metadata: Dict[str, object] = {
        "source": {
            "kind": "web_capture",
            "run_id": run_id,
            "url": url,
            "requirement_id": requirement_id,
        }
    }
    if metadata:
        item_metadata.update(metadata)

    item = new_ingest_item(
        source_type="website",
        source_uri=url,
        display_name=display_name or url,
        metadata=item_metadata,
    )
    item = item.with_status("completed")
    item.domain = domain
    engine = get_engine()

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO kb.ingest_items (
                    id,
                    job_id,
                    source_type,
                    source_uri,
                    display_name,
                    status,
                    metadata,
                    created_at,
                    updated_at
                ) VALUES (
                    :id,
                    :job_id,
                    :source_type,
                    :source_uri,
                    :display_name,
                    :status,
                    CAST(:metadata AS JSONB),
                    NOW(),
                    NOW()
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": str(item.id),
                "job_id": str(item.job_id) if item.job_id else str(uuid4()),
                "source_type": item.source_type,
                "source_uri": item.source_uri,
                "display_name": item.display_name,
                "status": item.status,
                "metadata": json.dumps(item_metadata, ensure_ascii=False),
            },
        )

    document_metadata = {
        "language": "und",
        "source": {
            "kind": "web_capture",
            "run_id": run_id,
            "url": url,
            "requirement_id": requirement_id,
        },
    }
    document = NormalizedDocument(
        ingest_item_id=item.id,
        markdown=markdown,
        text=plain_text,
        metadata=document_metadata,
    )

    chunk_defs = chunker.build_chunks(markdown, CHUNK_TOKENS_DEFAULT, OVERLAP_MAX_PCT_DEFAULT)
    chunks: List[Chunk] = []
    for definition in chunk_defs:
        chunk = Chunk(
            id=uuid4(),
            ingest_item_id=item.id,
            document_id=None,
            chunk_index=int(definition.get("idx", len(chunks))),
            heading_path=[str(definition.get("title", "Document"))],
            kind="web_capture",
            text=str(definition.get("text", "")),
            token_count=int(definition.get("token_count", 0)),
            overlap_tokens=int(definition.get("overlap", 0)),
            ner_entities=[],
        )
        chunks.append(chunk)

    chunk_texts = [chunk.text for chunk in chunks if chunk.text]
    if chunk_texts:
        chunk_summaries = summarize_chunks_with_gemini(chunk_texts, max_length=256)
    else:
        chunk_summaries = []
    for chunk, summary in zip(chunks, chunk_summaries):
        chunk.summary = summary
        item = item.with_chunk_summary(chunk.chunk_index, summary)

    if curated_summary:
        item = item.with_document_summary(curated_summary)
    else:
        try:
            generated_summary = summarize_with_gemini(plain_text, max_length=400)
            item = item.with_document_summary(generated_summary)
        except Exception:  # pragma: no cover - summarizer availability issues
            pass

    embeddings: List[ChunkEmbedding] = []
    if chunk_texts:
        runtime_config = get_runtime_config()
        general_model = runtime_config.active("active_emb_general")
        legal_model = runtime_config.active("active_emb_legal")
        code_model = runtime_config.active("active_emb_code")
        spaces_used = {general_model.name}
        dims_by_space = {general_model.name: general_model.require_dims()}
        general_vectors: List[List[float]] = []
        try:
            general_vectors = embed_with_general(chunk_texts)
        except Exception as exc:  # pragma: no cover - depends on external quota
            LOGGER.warning(
                "Falling back to empty general embeddings",
                extra={
                    "ingest_item": str(item.id),
                    "run_id": run_id,
                    "error": str(exc),
                },
            )
            general_vectors = []

        for vector, chunk in zip(general_vectors, chunks):
            embeddings.append(
                ChunkEmbedding(
                    chunk_id=chunk.id,
                    space=general_model.name,
                    model=general_model.identifier,
                    vector=vector,
                )
            )

        domain_key = (item.domain or domain or "web").lower()
        if domain_key == "legal":
            try:
                legal_vectors = embed_with_legal(chunk_texts)
            except Exception as exc:  # pragma: no cover - runtime availability
                LOGGER.warning(
                    "Legal embedding backend unavailable",
                    extra={
                        "ingest_item": str(item.id),
                        "run_id": run_id,
                        "error": str(exc),
                    },
                )
                legal_vectors = []
            for vector, chunk in zip(legal_vectors, chunks):
                embeddings.append(
                    ChunkEmbedding(
                        chunk_id=chunk.id,
                        space=legal_model.name,
                        model=legal_model.identifier,
                        vector=vector,
                    )
                )
            spaces_used.add(legal_model.name)
            dims_by_space[legal_model.name] = legal_model.require_dims()
        elif domain_key == "code":
            try:
                code_vectors = embed_with_code(chunk_texts)
            except Exception as exc:  # pragma: no cover - runtime availability
                LOGGER.warning(
                    "Code embedding backend unavailable",
                    extra={
                        "ingest_item": str(item.id),
                        "run_id": run_id,
                        "error": str(exc),
                    },
                )
                code_vectors = []
            for vector, chunk in zip(code_vectors, chunks):
                embeddings.append(
                    ChunkEmbedding(
                        chunk_id=chunk.id,
                        space=code_model.name,
                        model=code_model.identifier,
                        vector=vector,
                    )
                )
            spaces_used.add(code_model.name)
            dims_by_space[code_model.name] = code_model.require_dims()

        LOGGER.info(
            "web capture embeddings generated",
            extra={
                "ingest_item_id": str(item.id),
                "spaces_used": sorted(spaces_used),
                "dimensions": dims_by_space,
            },
        )

    abstractions = _build_abstractions(chunks)
    chunk_emotions = build_chunk_emotions(chunks)
    report = persist_results.fn(
        item,
        document,
        chunks,
        embeddings,
        chunk_emotions,
        abstractions,
    )
    kb_document_id = report.metadata.get("document_id")

    return UUID(kb_document_id) if kb_document_id else item.id, [chunk.id for chunk in chunks]


def _build_abstractions(chunks: Iterable[Chunk]) -> List[str]:
    abstractions: List[str] = []
    buffer: List[str] = []
    for chunk in chunks:
        snippet = chunk.summary or chunk.text
        if snippet:
            buffer.append(snippet)
        if len(buffer) == 3:
            abstractions.append(" ".join(buffer))
            buffer = []
    if buffer:
        abstractions.append(" ".join(buffer))
    return abstractions


__all__ = ["ingest_web_capture"]
