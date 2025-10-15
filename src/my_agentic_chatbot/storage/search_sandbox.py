"""Helpers for the agent.search_sandbox staging table."""

from __future__ import annotations

import json
from typing import Dict, Iterable, Optional, Sequence
from uuid import UUID

from sqlalchemy import text

from .db import get_engine


def _pg_uuid_array(values: Sequence[UUID] | Iterable[UUID]) -> str:
    return "{" + ",".join(str(value) for value in values) + "}"


class SearchSandboxTable:
    """Persistence helper for session-scoped web search results."""

    def __init__(self, *, engine=None) -> None:
        self._engine = engine or get_engine()

    def upsert_result(
        self,
        *,
        run_id: str,
        requirement_id: Optional[str],
        task_id: Optional[str],
        iteration: int,
        rank: int,
        source: str,
        title: Optional[str],
        url: Optional[str],
        snippet: Optional[str],
        raw_result: Dict[str, object],
    ) -> None:
        if not url:
            return
        payload = {
            "run_id": run_id,
            "requirement_id": requirement_id,
            "task_id": task_id,
            "iteration": iteration,
            "rank": rank,
            "source": source,
            "title": title,
            "url": url,
            "snippet": snippet,
            "raw_result": json.dumps(raw_result, ensure_ascii=False),
        }
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO agent.search_sandbox (
                        run_id,
                        requirement_id,
                        task_id,
                        iteration,
                        rank,
                        source,
                        title,
                        url,
                        snippet,
                        raw_result,
                        created_at,
                        updated_at
                    ) VALUES (
                        CAST(:run_id AS UUID),
                        :requirement_id,
                        :task_id,
                        :iteration,
                        :rank,
                        :source,
                        :title,
                        :url,
                        :snippet,
                        CAST(:raw_result AS JSONB),
                        NOW(),
                        NOW()
                    )
                    ON CONFLICT (run_id, iteration, url)
                    DO UPDATE SET
                        rank = LEAST(agent.search_sandbox.rank, EXCLUDED.rank),
                        title = COALESCE(EXCLUDED.title, agent.search_sandbox.title),
                        snippet = COALESCE(EXCLUDED.snippet, agent.search_sandbox.snippet),
                        raw_result = EXCLUDED.raw_result,
                        source = EXCLUDED.source,
                        requirement_id = COALESCE(EXCLUDED.requirement_id, agent.search_sandbox.requirement_id),
                        task_id = COALESCE(EXCLUDED.task_id, agent.search_sandbox.task_id),
                        updated_at = NOW()
                    """
                ),
                payload,
            )

    def mark_promoted(
        self,
        *,
        run_id: str,
        iteration: int,
        url: str,
        kb_document_id: Optional[UUID],
        kb_chunk_ids: Optional[Sequence[UUID]] = None,
    ) -> None:
        if not url:
            return
        payload = {
            "run_id": run_id,
            "iteration": iteration,
            "url": url,
            "kb_document_id": str(kb_document_id) if kb_document_id else None,
            "kb_chunk_ids": _pg_uuid_array(kb_chunk_ids or []),
        }
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE agent.search_sandbox
                    SET promoted = TRUE,
                        promoted_at = NOW(),
                        kb_document_id = :kb_document_id,
                        kb_chunk_ids = CASE
                            WHEN :kb_chunk_ids = '{}' THEN NULL
                            ELSE CAST(:kb_chunk_ids AS UUID[])
                        END,
                        updated_at = NOW()
                    WHERE run_id = CAST(:run_id AS UUID)
                      AND iteration = :iteration
                      AND url = :url
                    """
                ),
                payload,
            )


__all__ = ["SearchSandboxTable"]

