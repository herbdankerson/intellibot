"""Helpers for the agent.web_work_items staging table."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .db import get_engine


@dataclass
class WebWorkRecord:
    """In-memory representation of a staging-table entry."""

    id: UUID
    run_id: str
    requirement_id: Optional[str]
    task_id: Optional[str]
    query: str
    source_url: Optional[str]
    source_title: Optional[str]
    snippet: Optional[str] = None
    summary: Optional[str] = None
    markdown: Optional[str] = None
    curated: bool = False
    authority_score: Optional[float] = None
    topicality_score: Optional[float] = None
    locality_score: Optional[float] = None
    retrieval_score: Optional[float] = None
    query_embedding: List[float] = field(default_factory=list)
    snippet_embedding: List[float] = field(default_factory=list)
    kb_document_id: Optional[UUID] = None
    kb_chunk_ids: List[UUID] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def score(self) -> float:
        authority = self.authority_score or 0.0
        topicality = self.topicality_score or 0.0
        locality = self.locality_score or 0.0
        retrieval = self.retrieval_score or 0.0
        return authority * 0.4 + topicality * 0.3 + locality * 0.1 + retrieval * 0.2


class WebWorkTable:
    """Persistence helper for run-scoped web staging items."""

    def __init__(self, *, engine: Optional[Engine] = None) -> None:
        self._engine = engine or get_engine()

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    def cleanup_expired(self) -> None:
        """Delete rows whose TTL has elapsed."""

        with self._engine.begin() as conn:
            conn.execute(text("DELETE FROM agent.web_work_items WHERE expires_at < NOW()"))

    def reset_run(self, run_id: str) -> None:
        """Remove prior staging rows for the supplied run."""

        with self._engine.begin() as conn:
            conn.execute(text("DELETE FROM agent.web_work_items WHERE run_id = :run_id"), {"run_id": run_id})

    # ------------------------------------------------------------------
    # Mutation helpers
    # ------------------------------------------------------------------

    def insert_raw(
        self,
        *,
        run_id: str,
        requirement_id: Optional[str],
        task_id: Optional[str],
        query: str,
        source_url: Optional[str],
        source_title: Optional[str],
        raw_result: Dict[str, Any],
        snippet: Optional[str] = None,
        query_embedding: Optional[Sequence[float]] = None,
        snippet_embedding: Optional[Sequence[float]] = None,
        retrieval_score: Optional[float] = None,
    ) -> UUID:
        """Insert a raw SearxNG result prior to enrichment."""

        record_id = uuid4()
        payload = {
            "id": str(record_id),
            "run_id": run_id,
            "requirement_id": requirement_id,
            "task_id": task_id,
            "query": query,
            "source_url": source_url,
            "source_title": source_title,
            "raw_result": json.dumps(raw_result, ensure_ascii=False),
            "snippet": snippet,
            "query_embedding": _vector_literal(query_embedding),
            "snippet_embedding": _vector_literal(snippet_embedding),
            "retrieval_score": retrieval_score,
        }
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO agent.web_work_items (
                        id,
                        run_id,
                        requirement_id,
                        task_id,
                        query,
                        source_url,
                        source_title,
                        raw_result,
                        snippet,
                        query_embedding,
                        snippet_embedding,
                        retrieval_score,
                        created_at,
                        updated_at
                    ) VALUES (
                        :id,
                        :run_id,
                        :requirement_id,
                        :task_id,
                        :query,
                        :source_url,
                        :source_title,
                        CAST(:raw_result AS JSONB),
                        :snippet,
                        CAST(:query_embedding AS VECTOR(768)),
                        CAST(:snippet_embedding AS VECTOR(768)),
                        :retrieval_score,
                        NOW(),
                        NOW()
                    )
                    """
                ),
                payload,
            )
        return record_id

    def update_enrichment(
        self,
        record_id: UUID,
        *,
        fetch_status: str,
        http_status: Optional[int] = None,
        fetched_at: Optional[datetime] = None,
        rendered_at: Optional[datetime] = None,
        html: Optional[str] = None,
        markdown: Optional[str] = None,
        summary: Optional[str] = None,
        authority_score: Optional[float] = None,
        topicality_score: Optional[float] = None,
        locality_score: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Update enrichment details for a record."""

        params: Dict[str, Any] = {
            "id": str(record_id),
            "fetch_status": fetch_status,
            "http_status": http_status,
            "fetched_at": fetched_at or datetime.now(timezone.utc),
            "rendered_at": rendered_at,
            "html": html,
            "markdown": markdown,
            "summary": summary,
            "authority_score": authority_score,
            "topicality_score": topicality_score,
            "locality_score": locality_score,
            "metadata": json.dumps(metadata or {}, ensure_ascii=False),
        }
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE agent.web_work_items
                    SET fetch_status = :fetch_status,
                        http_status = :http_status,
                        fetched_at = :fetched_at,
                        rendered_at = :rendered_at,
                        html = :html,
                        markdown = :markdown,
                        summary = :summary,
                        authority_score = :authority_score,
                        topicality_score = :topicality_score,
                        locality_score = :locality_score,
                        metadata = CAST(:metadata AS JSONB),
                        updated_at = NOW()
                    WHERE id = :id
                    """
                ),
                params,
            )

    def mark_curated(
        self,
        record_id: UUID,
        *,
        reason: str,
        kb_document_id: Optional[UUID] = None,
        kb_chunk_ids: Optional[Iterable[UUID]] = None,
    ) -> None:
        """Mark a record as curated and link it to KB artefacts."""

        chunk_array = _pg_uuid_array(kb_chunk_ids or [])
        payload = {
            "id": str(record_id),
            "reason": reason,
            "kb_document_id": str(kb_document_id) if kb_document_id else None,
            "kb_chunk_ids": chunk_array,
        }
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE agent.web_work_items
                    SET curated = TRUE,
                        curated_reason = :reason,
                        kb_document_id = :kb_document_id,
                        kb_chunk_ids = CASE WHEN :kb_chunk_ids = '{}' THEN NULL ELSE CAST(:kb_chunk_ids AS UUID[]) END,
                        updated_at = NOW()
                    WHERE id = :id
                    """
                ),
                payload,
            )

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def curated_items(self, run_id: str, *, limit: int = 6) -> List[WebWorkRecord]:
        """Return curated items ordered by combined score."""

        with self._engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        id,
                        run_id,
                        requirement_id,
                        task_id,
                        query,
                        source_url,
                        source_title,
                        summary,
                        snippet,
                        markdown,
                        curated,
                        authority_score,
                        topicality_score,
                        locality_score,
                        kb_document_id,
                        kb_chunk_ids,
                        metadata,
                        retrieval_score,
                        query_embedding,
                        snippet_embedding
                    FROM agent.web_work_items
                    WHERE run_id = :run_id
                      AND curated = TRUE
                    ORDER BY COALESCE(authority_score, 0) * 0.5
                           + COALESCE(topicality_score, 0) * 0.35
                           + COALESCE(locality_score, 0) * 0.15 DESC,
                             updated_at DESC
                    LIMIT :limit
                    """
                ),
                {"run_id": run_id, "limit": limit},
            ).mappings()
            return [self._map_record(row) for row in rows]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _map_record(self, row: Any) -> WebWorkRecord:
        chunk_ids = _parse_uuid_array(row.get("kb_chunk_ids"))
        metadata_raw = row.get("metadata") or {}
        metadata: Dict[str, Any]
        if isinstance(metadata_raw, dict):
            metadata = metadata_raw
        else:
            try:
                metadata = json.loads(metadata_raw)
            except Exception:  # pragma: no cover - defensive
                metadata = {"raw": metadata_raw}
        return WebWorkRecord(
            id=row["id"],
            run_id=row["run_id"],
            requirement_id=row["requirement_id"],
            task_id=row["task_id"],
            query=row["query"],
            source_url=row["source_url"],
            source_title=row["source_title"],
            summary=row["summary"],
            snippet=row.get("snippet"),
            markdown=row["markdown"],
            curated=bool(row["curated"]),
            authority_score=_coerce_float(row.get("authority_score")),
            topicality_score=_coerce_float(row.get("topicality_score")),
            locality_score=_coerce_float(row.get("locality_score")),
            retrieval_score=_coerce_float(row.get("retrieval_score")),
            query_embedding=_coerce_vector(row.get("query_embedding")),
            snippet_embedding=_coerce_vector(row.get("snippet_embedding")),
            kb_document_id=row.get("kb_document_id"),
            kb_chunk_ids=chunk_ids,
            metadata=metadata,
        )

    def curated_count(self, run_id: str) -> int:
        with self._engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM agent.web_work_items
                    WHERE run_id = :run_id AND curated = TRUE
                    """
                ),
                {"run_id": run_id},
            ).scalar_one()
        return int(result)


def _pg_uuid_array(values: Iterable[UUID]) -> str:
    items = [str(value) for value in values]
    if not items:
        return "{}"
    return "{" + ",".join(items) + "}"


def _parse_uuid_array(raw: Any) -> List[UUID]:
    if raw in (None, "{}", [], ()):  # pragma: no cover - defensive flattening
        return []
    if isinstance(raw, list):
        return [UUID(str(value)) for value in raw]
    if isinstance(raw, str):
        stripped = raw.strip("{}")
        if not stripped:
            return []
        return [UUID(part) for part in stripped.split(",") if part]
    return []


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):  # pragma: no cover - guard against malformed rows
        return None


def _coerce_vector(value: Any) -> List[float]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [float(part) for part in value]
    if isinstance(value, str):
        stripped = value.strip("[]{}")
        if not stripped:
            return []
        return [float(part) for part in stripped.split(",") if part]
    if isinstance(value, memoryview):  # pgvector may yield memoryview
        return [float(part) for part in bytes(value)]
    return []


def _vector_literal(values: Optional[Sequence[float]]) -> Optional[str]:
    if values is None:
        return None
    components = ",".join(f"{float(part):.8f}" for part in values)
    return f"[{components}]"


__all__ = ["WebWorkRecord", "WebWorkTable"]
