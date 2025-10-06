"""Tests for the AgentRunLogger utility."""

from datetime import datetime

from src.my_agentic_chatbot.run_logging import AgentRunLogger
from src.my_agentic_chatbot.schemas import AgentResponse, EvidenceItem, EvidencePack, Plan, PlanTask
from src.my_agentic_chatbot.workflows import policies


def _sample_plan() -> Plan:
    task = PlanTask(
        id="db-1",
        description="Fetch data",
        tool="db_search",
        budget_tokens=policies.DEFAULT_DB_BUDGET_TOKENS,
        timeout_seconds=policies.DEFAULT_DB_TIMEOUT_SECONDS,
    )
    return Plan(goals=["Demo"], tasks=[task])


def _sample_evidence() -> EvidencePack:
    item = EvidenceItem(id="db-1", source="db", content="Row", score=1.0)
    return EvidencePack(items=[item], summary="Row", token_count=1)


def _sample_response() -> AgentResponse:
    return AgentResponse(answer="Done", citations=["db-1"], confidence=0.5)


def test_logger_buffers_events_when_disabled() -> None:
    logger = AgentRunLogger(enabled=False)
    logger.begin_run(user_message="hello")
    plan = _sample_plan()
    logger.log_plan(plan)
    logger.log_event("custom", {"value": 1})
    logger.log_evidence(_sample_evidence())
    logger.log_response(_sample_response())
    logger.finalize(success=True)

    assert any(event.event_type == "plan_generated" for event in logger.buffered_events)
    assert any(event.event_type == "custom" for event in logger.buffered_events)
    assert any(event.event_type == "run_completed" for event in logger.buffered_events)


def test_logger_finalizes_with_error_metadata() -> None:
    logger = AgentRunLogger(enabled=False)
    logger.begin_run(user_message="fault")
    logger.finalize(success=False, metadata={"error": "boom"})

    events = [event for event in logger.buffered_events if event.event_type == "run_completed"]
    assert events and events[0].payload["success"] is False
    assert events[0].payload["duration_ms"] >= 0
