"""Utilities for persisting chat transcripts into the knowledge base."""

from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional
from uuid import UUID, uuid4

from sqlalchemy import text

from etl.tasks.intake_models import (
    Chunk,
    ChunkEmbedding,
    IngestItem,
    NormalizedDocument,
    new_ingest_item,
)
from etl.tasks.model_clients import (
    embed_with_code,
    embed_with_general,
    embed_with_legal,
    summarize_chunks_with_gemini,
    summarize_with_gemini,
)
from etl.tasks.intake_tasks import build_chunk_emotions, persist_results

from .schemas import AgentResponse, EvidencePack, Plan
from .storage.connection import get_engine
from .runtime_config import get_runtime_config

LOGGER = logging.getLogger(__name__)


def persist_chat_transcript(
    run_id: UUID,
    user_message: str,
    response: AgentResponse,
    *,
    plan: Optional[Plan] = None,
    evidence: Optional[EvidencePack] = None,
    domain: str = "general",
) -> Optional[UUID]:
    """Store the chat interaction in ParadeDB and return the ingest item id."""

    try:
        display_name = _display_name_from_messages(user_message, response.answer)
        metadata: Dict[str, object] = {
            "source": {
                "kind": "chat",
                "run_id": str(run_id),
            },
            "citations": response.citations,
            "confidence": response.confidence,
            "unresolved": response.unresolved_questions,
        }
        if plan is not None:
            metadata["plan"] = plan.model_dump(mode="json")
        if evidence is not None:
            metadata["evidence"] = evidence.model_dump(mode="json")

        item = new_ingest_item(
            source_type="chat",
            source_uri=f"chat:{run_id}",
            display_name=display_name,
            metadata=metadata,
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
                    ON CONFLICT (id) DO UPDATE
                    SET status = EXCLUDED.status,
                        metadata = EXCLUDED.metadata,
                        updated_at = NOW()
                    """
                ),
                {
                    "id": str(item.id),
                    "job_id": str(item.job_id) if item.job_id else str(uuid4()),
                    "source_type": item.source_type,
                    "source_uri": item.source_uri,
                    "display_name": item.display_name,
                    "status": item.status,
                    "metadata": json.dumps(metadata),
                },
            )

        conversation_text = _conversation_text(user_message, response.answer)
        document = NormalizedDocument(
            ingest_item_id=item.id,
            markdown=conversation_text,
            text=conversation_text,
            metadata={
                "language": "und",
                "source": {
                    "kind": "chat",
                    "run_id": str(run_id),
                },
            },
        )

        chunks = _build_chunks(item, user_message, response.answer)
        summaries = summarize_chunks_with_gemini([chunk.text for chunk in chunks], max_length=200)
        for chunk, summary in zip(chunks, summaries):
            chunk.summary = summary
            item = item.with_chunk_summary(chunk.chunk_index, summary)

        try:
            doc_summary = summarize_with_gemini(conversation_text, max_length=400)
            item = item.with_document_summary(doc_summary)
        except Exception as exc:  # pragma: no cover - summarizer issues
            LOGGER.warning("Chat summarization failed: %s", exc)

        embeddings = _build_embeddings(item, chunks, domain=domain)
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

        entry_ids = report.metadata.get("entry_ids") if isinstance(report.metadata, dict) else None
        if entry_ids:
            extra_meta = {
                "chat": {
                    "run_id": str(run_id),
                    "confidence": response.confidence,
                    "citations": response.citations,
                }
            }
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        UPDATE kb.entries
                        SET session_id = :session_id,
                            meta = meta || CAST(:extra_meta AS JSONB)
                        WHERE id = ANY(:entry_ids)
                        """
                    ),
                    {
                        "session_id": str(run_id),
                        "extra_meta": json.dumps(extra_meta),
                        "entry_ids": list(map(str, entry_ids)),
                    },
                )

        LOGGER.info(
            "Persisted chat transcript %s as ingest item %s",
            run_id,
            report.ingest_item.id,
        )
        return report.ingest_item.id
    except Exception as exc:  # pragma: no cover - defensive logging
        LOGGER.warning("Failed to persist chat transcript %s: %s", run_id, exc, exc_info=True)
        return None


def _display_name_from_messages(user_message: str, answer: str) -> str:
    preview = (user_message or answer).strip().splitlines()[0][:80]
    if not preview:
        preview = "chat"
    return preview


def _conversation_text(user_message: str, answer: str) -> str:
    return f"User: {user_message}\n\nAssistant: {answer}"


def _build_chunks(item: IngestItem, user_message: str, answer: str) -> List[Chunk]:
    chunk_user = Chunk(
        id=uuid4(),
        ingest_item_id=item.id,
        document_id=None,
        chunk_index=0,
        heading_path=["chat", "user"],
        kind="user_message",
        text=user_message,
        token_count=_estimate_tokens(user_message),
        overlap_tokens=0,
        ner_entities=[],
    )
    chunk_assistant = Chunk(
        id=uuid4(),
        ingest_item_id=item.id,
        document_id=None,
        chunk_index=1,
        heading_path=["chat", "assistant"],
        kind="assistant_message",
        text=answer,
        token_count=_estimate_tokens(answer),
        overlap_tokens=0,
        ner_entities=[],
    )
    return [chunk_user, chunk_assistant]


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text.split()))


def _build_embeddings(item: IngestItem, chunks: List[Chunk], *, domain: str) -> List[ChunkEmbedding]:
    texts = [chunk.text for chunk in chunks]
    embeddings: List[ChunkEmbedding] = []
    runtime_config = get_runtime_config()
    general_model = runtime_config.active("active_emb_general")
    legal_model = runtime_config.active("active_emb_legal")
    code_model = runtime_config.active("active_emb_code")

    general_vectors = embed_with_general(texts)
    for vector, chunk in zip(general_vectors, chunks):
        embeddings.append(
            ChunkEmbedding(
                chunk_id=chunk.id,
                space=general_model.name,
                model=general_model.identifier,
                vector=vector,
            )
        )

    spaces_used = {general_model.name}
    dims_by_space = {general_model.name: general_model.require_dims()}

    domain_key = (item.domain or domain or "general").lower()
    if domain_key == "legal":
        legal_vectors = embed_with_legal(texts)
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
        code_vectors = embed_with_code(texts)
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
        "chat embeddings generated",
        extra={
            "ingest_item_id": str(item.id),
            "spaces_used": sorted(spaces_used),
            "dimensions": dims_by_space,
        },
    )

    return embeddings


def _build_abstractions(chunks: List[Chunk]) -> List[str]:
    abstractions: List[str] = []
    group: List[str] = []
    for chunk in chunks:
        snippet = chunk.summary or chunk.text
        if snippet:
            group.append(snippet)
        if len(group) == 3:
            abstractions.append(" ".join(group))
            group = []
    if group:
        abstractions.append(" ".join(group))
    return abstractions


__all__ = ["persist_chat_transcript"]
