"""FastAPI entry point for the agentic chatbot."""

from __future__ import annotations

from fastapi import FastAPI

from .config import get_settings
from .logging_conf import configure_logging
from .planner.main_planner import plan_from_message
from .response.responder import Responder
from .schemas import AgentResponse, UserQuery
from .workflows.orchestrator import WorkflowOrchestrator


def create_app() -> FastAPI:
    """Instantiate and configure the FastAPI application."""

    configure_logging()
    settings = get_settings()
    orchestrator = WorkflowOrchestrator(responder=Responder(model_name=settings.responder_model))

    app = FastAPI(title="Agentic Chatbot", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "planner_model": settings.planner_model}

    @app.post("/run", response_model=AgentResponse)
    def run(query: UserQuery) -> AgentResponse:
        plan = plan_from_message(query.message, model=settings.planner_model)
        response, _ = orchestrator.run_pipeline(query.message, plan)
        return response

    return app


app = create_app()


__all__ = ["app", "create_app"]
