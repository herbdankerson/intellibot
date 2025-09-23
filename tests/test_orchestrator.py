"""Tests for the workflow orchestrator."""

from src.my_agentic_chatbot.planner.main_planner import plan_from_message
from src.my_agentic_chatbot.schemas import AgentResponse, Plan, TaskStatus
from src.my_agentic_chatbot.workflows.approver import ApprovalResult, Approver
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


class RejectingApprover(Approver):
    """Approver that rejects the final response for testing."""

    def approve_response(self, response: AgentResponse) -> ApprovalResult:
        return ApprovalResult(approved=False, reason="needs review")


def test_run_pipeline_rejects_final_response() -> None:
    orchestrator = WorkflowOrchestrator(approver=RejectingApprover())
    response, result = orchestrator.run_pipeline("hello", Plan())

    assert result.report.successful is False
    assert any(
        event.task_id == "final_response" and event.status == TaskStatus.FAILED
        for event in result.report.events
    )
    assert result.report.notes[-1] == "Response rejected: needs review"
    assert response.answer == "Final response requires revision before delivery."
    assert response.confidence == 0.0
