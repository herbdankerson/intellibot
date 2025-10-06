"""Structured logging helpers for agent runs."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .schemas import AgentResponse, AuditReport, EvidenceItem, EvidencePack, Plan
from .storage.db import get_engine

LOGGER = logging.getLogger(__name__)


@dataclass
class BufferedEvent:
    """In-memory representation of a run event (fallback when DB unavailable)."""

    event_type: str
    payload: Dict[str, Any]
    task_id: Optional[str] = None
    tool: Optional[str] = None
    status: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class AgentRunLogger:
    """Persist structured run telemetry to the database and local buffer."""

    def __init__(
        self,
        *,
        run_id: Optional[UUID] = None,
        engine: Optional[Engine] = None,
        enabled: bool = True,
    ) -> None:
        self.run_id = run_id or uuid4()
        self.engine = engine or get_engine()
        self.enabled = enabled
        self.started_at = datetime.now(timezone.utc)
        self._buffer: List[BufferedEvent] = []
        self._metadata: Dict[str, Any] = {}

    # ---------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------

    def begin_run(
        self,
        *,
        user_message: str,
        planner_model: Optional[str] = None,
        responder_model: Optional[str] = None,
        audit_model: Optional[str] = None,
        user_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record the start of a run."""

        payload = {
            "id": str(self.run_id),
            "user_message": user_message,
            "planner_model": planner_model,
            "responder_model": responder_model,
            "audit_model": audit_model,
            "user_metadata": user_metadata or {},
        }
        self._metadata = payload
        self._execute(
            text(
                """
                INSERT INTO agent.runs (
                    id,
                    user_message,
                    planner_model,
                    responder_model,
                    audit_model,
                    user_metadata,
                    started_at
                ) VALUES (
                    :id,
                    :user_message,
                    :planner_model,
                    :responder_model,
                    :audit_model,
                    CAST(:user_metadata AS JSONB),
                    NOW()
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            payload,
        )
        self._buffer.append(
            BufferedEvent(
                event_type="run_started",
                payload={"user_message": user_message},
            )
        )

    def log_plan(self, plan: Plan, *, raw_response: Optional[str] = None) -> None:
        payload = {
            "plan": plan.model_dump(),
            "raw_response": raw_response,
        }
        self._update_run({"plan": payload["plan"]})
        self.log_event("plan_generated", payload)

    def log_event(
        self,
        event_type: str,
        payload: Dict[str, Any],
        *,
        task_id: Optional[str] = None,
        tool: Optional[str] = None,
        status: Optional[str] = None,
    ) -> None:
        buffered = BufferedEvent(
            event_type=event_type,
            payload=payload,
            task_id=task_id,
            tool=tool,
            status=status,
        )
        self._buffer.append(buffered)
        self._execute(
            text(
                """
                INSERT INTO agent.events (
                    run_id,
                    event_type,
                    task_id,
                    tool,
                    status,
                    payload
                ) VALUES (
                    :run_id,
                    :event_type,
                    :task_id,
                    :tool,
                    :status,
                    CAST(:payload AS JSONB)
                )
                """
            ),
            {
                "run_id": str(self.run_id),
                "event_type": event_type,
                "task_id": task_id,
                "tool": tool,
                "status": status,
                "payload": json.dumps(payload),
            },
        )

    def log_tool_result(
        self,
        *,
        task_id: str,
        tool: str,
        status: str,
        inputs: Optional[Dict[str, Any]] = None,
        outputs: Optional[Iterable[EvidenceItem]] = None,
        error: Optional[str] = None,
    ) -> None:
        payload: Dict[str, Any] = {}
        if inputs is not None:
            payload["inputs"] = inputs
        if outputs is not None:
            payload["outputs"] = [item.model_dump() for item in outputs]
        if error is not None:
            payload["error"] = error
        self.log_event(
            "tool_execution",
            payload,
            task_id=task_id,
            tool=tool,
            status=status,
        )

    def log_evidence(self, pack: EvidencePack) -> None:
        payload = pack.model_dump()
        self._update_run({"evidence": payload})
        self.log_event("evidence_pack", payload)

    def log_audit(self, report: AuditReport) -> None:
        payload = report.model_dump()
        self._update_run({"audit_report": payload})
        self.log_event("audit_report", payload)

    def log_response(self, response: AgentResponse) -> None:
        payload = response.model_dump()
        self._update_run({"response": payload})
        self.log_event("responder_output", payload)

    def finalize(
        self,
        *,
        success: bool,
        response: Optional[AgentResponse] = None,
        audit_report: Optional[AuditReport] = None,
        evidence: Optional[EvidencePack] = None,
        metadata: Optional[Dict[str, Any]] = None,
        chat_ingest_item_id: Optional[UUID] = None,
    ) -> None:
        completed_at = datetime.now(timezone.utc)
        duration_ms = int((completed_at - self.started_at).total_seconds() * 1000)
        update: Dict[str, Any] = {
            "success": success,
            "completed_at": completed_at.isoformat(),
            "duration_ms": duration_ms,
            "metadata": metadata or {},
            "chat_ingest_item_id": str(chat_ingest_item_id) if chat_ingest_item_id else None,
        }
        if response is not None:
            update["response"] = response.model_dump()
        if audit_report is not None:
            update["audit_report"] = audit_report.model_dump()
        if evidence is not None:
            update["evidence"] = evidence.model_dump()
        self._update_run(update)
        self.log_event(
            "run_completed",
            {
                "success": success,
                "duration_ms": duration_ms,
                "chat_ingest_item_id": str(chat_ingest_item_id) if chat_ingest_item_id else None,
            },
        )

    # ------------------------------------------------------------------
    # Accessors / diagnostics
    # ------------------------------------------------------------------

    @property
    def buffered_events(self) -> List[BufferedEvent]:
        return list(self._buffer)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _execute(self, statement, params: Dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            with self.engine.begin() as connection:
                connection.execute(statement, params)
        except Exception as exc:  # pragma: no cover - defensive fallback
            self.enabled = False
            LOGGER.warning(
                "AgentRunLogger disabled after database error: %s", exc, exc_info=True
            )

    def _update_run(self, values: Dict[str, Any]) -> None:
        assignments: List[str] = []
        payload: Dict[str, Any] = {"id": str(self.run_id)}
        for key, value in values.items():
            column = key
            if value is None:
                assignments.append(f"{column} = NULL")
            elif column in {"plan", "response", "audit_report", "evidence", "metadata"}:
                assignments.append(f"{column} = CAST(:{column} AS JSONB)")
                payload[column] = json.dumps(value)
            elif column == "completed_at":
                assignments.append(f"{column} = CAST(:{column} AS TIMESTAMPTZ)")
                payload[column] = value
            elif column == "chat_ingest_item_id":
                assignments.append(f"{column} = CAST(:{column} AS UUID)")
                payload[column] = value
            else:
                assignments.append(f"{column} = :{column}")
                payload[column] = value
        if not assignments:
            return
        sql = f"UPDATE agent.runs SET {', '.join(assignments)}, updated_at = NOW() WHERE id = :id"
        self._execute(text(sql), payload)


__all__ = ["AgentRunLogger"]
