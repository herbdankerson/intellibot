"""FastAPI entry point for the agentic chatbot."""

from __future__ import annotations

import anyio
from functools import partial

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from .config import get_settings
from .logging_conf import configure_logging
from .planner.main_planner import plan_from_message
from .response.responder import Responder
from .run_logging import AgentRunLogger
from .chat_store import persist_chat_transcript
from .schemas import AgentResponse, IngestJobStatus, UrlIngestRequest, UserQuery
from .workflows.orchestrator import WorkflowOrchestrator
from .ingestion.service import IngestionService


def create_app() -> FastAPI:
    """Instantiate and configure the FastAPI application."""

    configure_logging()
    settings = get_settings()
    orchestrator = WorkflowOrchestrator(responder=Responder(model_name=settings.responder_model))
    ingestion_service = IngestionService()

    app = FastAPI(title="Agentic Chatbot", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "planner_model": settings.planner_model}

    @app.post("/run", response_model=AgentResponse)
    def run(query: UserQuery) -> AgentResponse:
        audit_model = None
        if orchestrator.audit_agent and orchestrator.audit_agent.agent_config:
            audit_model = orchestrator.audit_agent.agent_config.model

        run_logger = AgentRunLogger()
        run_logger.begin_run(
            user_message=query.message,
            planner_model=settings.planner_model,
            responder_model=settings.responder_model,
            audit_model=audit_model,
        )

        try:
            plan = plan_from_message(
                query.message,
                model=settings.planner_model,
                logger=run_logger,
            )
            response, result = orchestrator.run_pipeline(
                query.message,
                plan,
                run_logger=run_logger,
            )

            ingest_item_id = persist_chat_transcript(
                run_logger.run_id,
                query.message,
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
        except Exception as exc:
            run_logger.finalize(success=False, metadata={"error": str(exc)})
            raise

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

    return app


app = create_app()


__all__ = ["app", "create_app"]
