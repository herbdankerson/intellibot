"""FastAPI entry point for the agentic chatbot with OpenAI compatibility."""

from __future__ import annotations

import json
import time
import uuid
from functools import partial
from typing import Any, Dict, Iterable, Optional, cast

import anyio
from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import text
from pydantic import BaseModel

from .chat_store import persist_chat_transcript
from .config import get_settings
from .runtime_config import get_runtime_config
from .ingestion.service import IngestionService
from .logging_conf import configure_logging
from .llm_calls.llm_client import push_run_logger, reset_run_logger
from .planner.main_planner import plan_from_message
from .response.responder import Responder
from .run_logging import AgentRunLogger
from .schemas import AgentResponse, IngestJobStatus, UrlIngestRequest, UserQuery
from .workflows.orchestrator import WorkflowOrchestrator
from .storage.connection import get_engine


class FlowExecutionRequest(BaseModel):
    deployment_name: str
    parameters: Optional[Dict[str, Any]] = None
    wait_for_completion: bool = False


class FlowExecutionResponse(BaseModel):
    deployment_name: str
    state_name: str
    state_type: str
    flow_run_id: Optional[str] = None
    result: Optional[Any] = None
    result_error: Optional[str] = None


FlowExecutionRequest.model_rebuild()
FlowExecutionResponse.model_rebuild()


def create_app() -> FastAPI:
    """Instantiate and configure the FastAPI application."""

    configure_logging()
    settings = get_settings()
    runtime_config = get_runtime_config()
    planner_model_cfg = runtime_config.active("active_planner_model")
    responder_model_cfg = runtime_config.active("active_responder_model")
    planner_identifier = planner_model_cfg.identifier
    responder_identifier = responder_model_cfg.identifier

    orchestrator = WorkflowOrchestrator(responder=Responder())
    ingestion_service = IngestionService()
    engine = get_engine()

    def _record_event(
        raw: Dict[str, Any],
        *,
        run_id: Optional[str] = None,
        session_id: Optional[uuid.UUID] = None,
    ) -> None:
        payload = {
            "session_id": session_id,
            "run_id": run_id,
            "raw": json.dumps(raw),
        }
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO agent.events (session_id, run_id, raw)
                    VALUES (:session_id, :run_id, CAST(:raw AS JSONB))
                    """
                ),
                payload,
            )

    def _create_session(agent_name: str, meta: Dict[str, Any] | None = None) -> uuid.UUID:
        session_id = uuid.uuid4()
        payload = {
            "id": session_id,
            "agent_name": agent_name,
            "meta": json.dumps(meta or {}),
        }
        with engine.begin() as connection:
            result = connection.execute(
                text(
                    """
                    INSERT INTO agent.sessions (id, agent_id, meta)
                    SELECT :id, a.id, CAST(:meta AS JSONB)
                    FROM cfg.agents AS a
                    WHERE a.name = :agent_name
                    """
                ),
                payload,
            )
            if result.rowcount == 0:
                raise HTTPException(
                    status_code=404,
                    detail=f"Agent '{agent_name}' is not registered",
                )
        return session_id

    def _jsonable(value: Any) -> Any:
        try:
            json.dumps(value)
            return value
        except (TypeError, ValueError):
            if hasattr(value, "model_dump"):
                try:
                    data = value.model_dump(mode="json")  # type: ignore[attr-defined]
                    json.dumps(data)
                    return data
                except Exception:  # pragma: no cover - fall back to string
                    return str(value)
            return str(value)

    app = FastAPI(title="Agentic Chatbot", version="0.2.0")

    def _run_pipeline(message: str) -> tuple[AgentResponse, AgentRunLogger]:
        audit_model = None
        if orchestrator.audit_agent and orchestrator.audit_agent.agent_config:
            audit_model = orchestrator.audit_agent.agent_config.model

        run_logger = AgentRunLogger()
        run_logger.begin_run(
            user_message=message,
            planner_model=planner_model_cfg.name,
            responder_model=responder_model_cfg.name,
            audit_model=audit_model,
        )

        token = push_run_logger(run_logger)
        try:
            plan = plan_from_message(
                message,
                model=planner_identifier,
                logger=run_logger,
            )
            response, result = orchestrator.run_pipeline(
                message,
                plan,
                run_logger=run_logger,
            )

            ingest_item_id = persist_chat_transcript(
                run_logger.run_id,
                message,
                response,
                plan=plan,
                evidence=result.evidence,
                domain="general",
            )

            success = bool(
                result.report.successful
                and (result.audit_report is None or result.audit_report.passed)
            )
            run_logger.finalize(
                success=success,
                response=response,
                audit_report=result.audit_report,
                evidence=result.evidence,
                metadata={"execution_report": result.report.model_dump(mode="json")},
                chat_ingest_item_id=ingest_item_id,
            )

            return response, run_logger
        except Exception as exc:  # pragma: no cover - defensive
            run_logger.finalize(success=False, metadata={"error": str(exc)})
            raise
        finally:
            reset_run_logger(token)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "planner_model": planner_model_cfg.name}

    @app.post("/run", response_model=AgentResponse)
    def run(query: UserQuery) -> AgentResponse:
        response, _ = _run_pipeline(query.message)
        return response

    @app.post("/chat", response_model=AgentResponse)
    def chat(query: UserQuery) -> AgentResponse:
        session_id = _create_session("default-chat", {"source": "api"})
        try:
            response, run_logger = _run_pipeline(query.message)
        except Exception as exc:
            _record_event(
                {
                    "event": "chat_error",
                    "message": query.message,
                    "error": str(exc),
                },
                session_id=session_id,
            )
            raise
        _record_event(
            {
                "event": "chat_completed",
                "message": query.message,
                "run_id": str(run_logger.run_id),
            },
            run_id=str(run_logger.run_id),
            session_id=session_id,
        )
        return response

    @app.post("/logs/litellm")
    def litellm_logs(payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
        run_id = payload.get("metadata", {}).get("run_id")
        session_id_value = payload.get("metadata", {}).get("session_id")
        session_uuid: Optional[uuid.UUID] = None
        if isinstance(session_id_value, str) and session_id_value:
            try:
                session_uuid = uuid.UUID(session_id_value)
            except ValueError:
                session_uuid = None
        _record_event(
            {"event": "litellm_callback", "payload": payload},
            run_id=run_id,
            session_id=session_uuid,
        )
        return {"status": "ok"}

    @app.post("/freshbot/flows/execute", response_model=FlowExecutionResponse)
    def execute_freshbot_flow(
        request: FlowExecutionRequest = Body(...),
    ) -> FlowExecutionResponse:
        try:
            from prefect.deployments import run_deployment
            from prefect.exceptions import ObjectNotFound
        except Exception as exc:  # pragma: no cover - Prefect missing
            raise HTTPException(status_code=503, detail=f"Prefect unavailable: {exc}") from exc

        run_kwargs = {
            "name": request.deployment_name,
            "parameters": request.parameters or {},
        }
        use_return_state = bool(request.wait_for_completion)
        if use_return_state:
            run_kwargs["return_state"] = True

        try:
            flow_run = run_deployment(**run_kwargs)
        except ObjectNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail=f"Deployment '{request.deployment_name}' not found",
            ) from exc
        except TypeError:
            use_return_state = False
            flow_run = run_deployment(
                name=request.deployment_name,
                parameters=request.parameters or {},
            )

        result_value: Optional[Any] = None
        result_error: Optional[str] = None
        state_obj: Optional[Any]
        state_name: Optional[str] = None
        state_type: Optional[str] = None
        flow_run_id: Optional[str] = None

        if use_return_state and hasattr(flow_run, "result"):
            state_obj = flow_run
            details = getattr(flow_run, "state_details", None)
            flow_run_id = getattr(details, "flow_run_id", None)
            state_name = getattr(flow_run, "name", None)
            state_type_obj = getattr(flow_run, "type", None)
            if state_type_obj is not None:
                state_type = getattr(state_type_obj, "value", None) or str(state_type_obj)
        else:
            state_obj = getattr(flow_run, "state", None)
            flow_run_id = getattr(flow_run, "id", None) or getattr(flow_run, "flow_run_id", None)
            state_name = cast(Optional[str], getattr(flow_run, "state_name", None))
            if not state_name and state_obj is not None:
                state_name = getattr(state_obj, "name", None)
            state_type_obj = getattr(flow_run, "state_type", None)
            if state_type_obj is not None:
                state_type = getattr(state_type_obj, "value", None) or str(state_type_obj)
            elif state_obj is not None:
                fallback_type = getattr(state_obj, "type", None)
                if fallback_type is not None:
                    state_type = getattr(fallback_type, "value", None) or str(fallback_type)

        if request.wait_for_completion and state_obj is not None:
            try:
                result_value = _jsonable(state_obj.result())
            except Exception as exc:  # pragma: no cover - Prefect surfaces runtime errors
                result_error = str(exc)

        return FlowExecutionResponse(
            deployment_name=request.deployment_name,
            state_name=state_name or "unknown",
            state_type=state_type or "unknown",
            flow_run_id=str(flow_run_id) if flow_run_id else None,
            result=result_value,
            result_error=result_error,
        )

    @app.post("/approve/{task_id}")
    def approve_task(task_id: uuid.UUID) -> Dict[str, str]:
        with engine.begin() as connection:
            result = connection.execute(
                text(
                    """
                    UPDATE agent.tasks
                    SET status = 'approved', resolved_at = NOW()
                    WHERE id = :task_id
                    """
                ),
                {"task_id": task_id},
            )
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="Task not found")
        _record_event(
            {
                "event": "task_approved",
                "task_id": str(task_id),
            },
        )
        return {"status": "approved", "task_id": str(task_id)}

    @app.post("/api/v1/documents/upload", response_model=IngestJobStatus)
    async def upload_document(
        file: UploadFile = File(...),
        display_name: str | None = None,
    ) -> IngestJobStatus:
        payload = await file.read()
        if not payload:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")
        name = display_name or file.filename or "document"
        metadata = {"content_type": file.content_type}
        job = partial(
            ingestion_service.ingest_bytes,
            source_type="document",
            source_uri=file.filename or name,
            display_name=name,
            payload=payload,
            extra_metadata=metadata,
        )
        status = await anyio.to_thread.run_sync(job)
        return status

    @app.post("/api/v1/documents/url", response_model=list[IngestJobStatus])
    async def ingest_urls(request: UrlIngestRequest) -> list[IngestJobStatus]:
        statuses: list[IngestJobStatus] = []
        for url in request.urls:
            metadata = {"source_url": url, "crawl_depth": request.crawl_depth}
            job = partial(
                ingestion_service.ingest_bytes,
                source_type="website",
                source_uri=url,
                display_name=url,
                payload=None,
                extra_metadata=metadata,
            )
            status = await anyio.to_thread.run_sync(job)
            statuses.append(status)
        return statuses

    @app.get("/api/v1/documents/jobs/{job_id}", response_model=IngestJobStatus)
    async def get_job_status(job_id: str) -> IngestJobStatus:
        status = ingestion_service.fetch_status(job_id)
        if not status:
            raise HTTPException(status_code=404, detail="Job not found")
        return status

    @app.post("/openai/verify")
    def openai_verify(_: Dict[str, Any] | None = Body(default=None)) -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/models")
    def openai_list_models() -> Dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": "agentic-orchestrator",
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "intellibot",
                }
            ],
        }

    def _extract_user_message(messages: Iterable[Dict[str, Any]]) -> str:
        history = list(messages)
        for message in reversed(history):
            if message.get("role") != "user":
                continue
            content = message.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    segment.get("text", "")
                    for segment in content
                    if isinstance(segment, dict) and segment.get("type") == "text"
                )
            if isinstance(content, str) and content.strip():
                return content.strip()
        raise HTTPException(status_code=400, detail="No user message provided")

    @app.post("/v1/chat/completions")
    def openai_chat_completions(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise HTTPException(status_code=400, detail="messages must be a list")

        user_message = _extract_user_message(messages)
        agent_response, _ = _run_pipeline(user_message)

        created = int(time.time())
        completion_id = f"chatcmpl-{uuid.uuid4()}"
        answer = agent_response.answer or ""

        choice: Dict[str, Any] = {
            "index": 0,
            "message": {"role": "assistant", "content": answer},
            "finish_reason": "stop",
        }
        if agent_response.citations:
            choice["message"]["metadata"] = {"citations": agent_response.citations}

        result = {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": payload.get("model", "agentic-orchestrator"),
            "choices": [choice],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }
        return result

    return app


app = create_app()


__all__ = ["app", "create_app"]
