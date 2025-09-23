"""Tests for the workflow orchestrator."""

from src.my_agentic_chatbot.planner.main_planner import plan_from_message
from src.my_agentic_chatbot.workflows.orchestrator import WorkflowOrchestrator


def test_orchestrator_executes_plan() -> None:
    orchestrator = WorkflowOrchestrator()
    plan = plan_from_message("Describe the evidence pack process")
    result = orchestrator.execute_plan(plan)
    assert result.evidence.items, "Orchestrator should collect evidence"
    assert result.report.events, "Execution report should track events"
    assert result.report.successful is True
    statuses = {event.status for event in result.report.events}
    assert "pending" not in {status.value for status in statuses}
