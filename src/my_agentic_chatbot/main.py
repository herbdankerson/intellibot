"""FastAPI entry point for the agentic chatbot with OpenAI compatibility."""

from __future__ import annotations

import time
import uuid
from functools import partial
from typing import Any, Dict, Iterable

import anyio
from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from .chat_store import persist_chat_transcript
from .config import get_settings
from .ingestion.service import IngestionService
from .logging_conf import configure_logging
from .planner.main_planner import plan_from_message
from .response.responder import Responder
from .run_logging import AgentRunLogger
from .schemas import AgentResponse, IngestJobStatus, UrlIngestRequest, UserQuery
from .workflows.orchestrator import WorkflowOrchestrator


def create_app() -> FastAPI:
    """Instantiate and configure the FastAPI application."""

    configure_logging()
    settings = get_settings()
    orchestrator = WorkflowOrchestrator(responder=Responder(model_name=settings.responder_model))
    ingestion_service = IngestionService()

    app = FastAPI(title="Agentic Chatbot", version="0.2.0")

    def _run_pipeline(message: str) -> AgentResponse:
        audit_model = None
        if orchestrator.audit_agent and orchestrator.audit_agent.agent_config:
            audit_model = orchestrator.audit_agent.agent_config.model

        run_logger = AgentRunLogger()
        run_logger.begin_run(
            user_message=message,
            planner_model=settings.planner_model,
            responder_model=settings.responder_model,
            audit_model=audit_model,
        )

        try:
            plan = plan_from_message(
                message,
                model=settings.planner_model,
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

            return response
        except Exception as exc:  # pragma: no cover - defensive
            run_logger.finalize(success=False, metadata={"error": str(exc)})
            raise

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "planner_model": settings.planner_model}

    @app.post("/run", response_model=AgentResponse)
    def run(query: UserQuery) -> AgentResponse:
        return _run_pipeline(query.message)

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
        agent_response = _run_pipeline(user_message)

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
