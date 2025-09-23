"""Tests for the workflow orchestrator."""

import json

from src.my_agentic_chatbot.schemas import (
    AgentResponse,
    EvidenceItem,
    Plan,
    PlanTask,
    TaskStatus,
)
from src.my_agentic_chatbot.workflows.approver import ApprovalResult, Approver
from src.my_agentic_chatbot.workflows.orchestrator import WorkflowOrchestrator
from src.my_agentic_chatbot.response.responder import Responder
from src.my_agentic_chatbot.workflows import policies


class StubDatabaseTool:
    def __init__(self, items: list[EvidenceItem]) -> None:
        self.items = items
        self.queries = []

    def search(self, query: str, limit: int | None = None):
        self.queries.append(query)
        return self.items[: limit or len(self.items)]


class RejectingApprover(Approver):
    """Approver that rejects the final response for testing."""

    def approve_response(self, response: AgentResponse) -> ApprovalResult:
        return ApprovalResult(approved=False, reason="needs review")


class StubResponderClient:
    def chat(self, messages):
        return json.dumps(
            {
                "answer": "Approved answer [db-1]",
                "citations": ["db-1"],
                "confidence": 0.6,
                "unresolved_questions": [],
            }
        )


def build_plan() -> Plan:
    return Plan(
        goals=["Describe the evidence pack process"],
        tasks=[
            PlanTask(
                id="db-1",
                description="Retrieve evidence",
                tool="db_search",
                budget_tokens=policies.DEFAULT_DB_BUDGET_TOKENS,
                timeout_seconds=policies.DEFAULT_DB_TIMEOUT_SECONDS,
            )
        ],
    )


def test_orchestrator_executes_plan() -> None:
    evidence = [
        EvidenceItem(id="db-1", source="db", content="Snippet", score=1.0),
        EvidenceItem(id="db-2", source="db", content="Snippet 2", score=0.5),
    ]
    orchestrator = WorkflowOrchestrator(
        db_tool=StubDatabaseTool(evidence),
        responder=Responder(client=StubResponderClient()),
    )
    plan = build_plan()
    result = orchestrator.execute_plan(plan)
    assert result.evidence.items, "Orchestrator should collect evidence"
    assert result.report.events, "Execution report should track events"
    assert result.report.successful is True
    statuses = {event.status for event in result.report.events}
    assert TaskStatus.PENDING not in statuses


def test_run_pipeline_rejects_final_response() -> None:
    orchestrator = WorkflowOrchestrator(
        db_tool=StubDatabaseTool(
            [EvidenceItem(id="db-1", source="db", content="Snippet", score=1.0)]
        ),
        responder=Responder(client=StubResponderClient()),
        approver=RejectingApprover(),
    )
    response, result = orchestrator.run_pipeline("hello", build_plan())

    assert result.report.successful is False
    assert any(
        event.task_id == "final_response" and event.status == TaskStatus.FAILED
        for event in result.report.events
    )
    assert result.report.notes[-1] == "Response rejected: needs review"
    assert response.answer == "Final response requires revision before delivery."
    assert response.confidence == 0.0
