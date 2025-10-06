"""Tests for the workflow orchestrator."""

import json

from src.my_agentic_chatbot.schemas import (
    AgentResponse,
    AuditFinding,
    AuditReport,
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
        self.queries: list[str] = []

    def search(self, query: str, limit: int | None = None):
        self.queries.append(query)
        return self.items[: limit or len(self.items)]

    def execute(self, task: PlanTask, *, limit: int | None = None):
        query = task.inputs.get("query") if isinstance(task.inputs, dict) else None
        self.queries.append(query or task.description)
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


class StubAuditAgent:
    def __init__(self, passed: bool = True) -> None:
        self.passed = passed

    def evaluate(self, *, question: str, plan: Plan, evidence, response) -> AuditReport:  # type: ignore[override]
        coverage = {criterion: self.passed for criterion in plan.acceptance_criteria}
        severity = "info" if self.passed else "error"
        message = "Audit ok" if self.passed else "Audit failed"
        finding = AuditFinding(
            severity=severity,
            message=message,
            citations=response.citations if response else [],
            acceptance_criteria=list(coverage.keys()),
        )
        return AuditReport(passed=self.passed, summary=message, coverage=coverage, findings=[finding])


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
        audit_agent=StubAuditAgent(),
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
        audit_agent=StubAuditAgent(),
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


class StubAgentRunner:
    def __init__(self) -> None:
        self.calls: list[PlanTask] = []

    def execute(self, task: PlanTask):
        self.calls.append(task)
        return [
            EvidenceItem(
                id="agent-1",
                source="agent:stub",
                content="Agent produced evidence",
                score=0.8,
            )
        ]


def test_orchestrator_executes_custom_agent() -> None:
    custom_runner = StubAgentRunner()
    orchestrator = WorkflowOrchestrator(
        db_tool=StubDatabaseTool([]),
        responder=Responder(client=StubResponderClient()),
        custom_agents={"agent-demo": custom_runner},
        audit_agent=StubAuditAgent(),
    )
    plan = Plan(
        goals=["Use custom agent"],
        tasks=[
            PlanTask(
                id="agent-demo",
                description="Delegate to custom agent",
                tool="agent-demo",
                budget_tokens=policies.DEFAULT_DB_BUDGET_TOKENS,
                timeout_seconds=policies.DEFAULT_TOOL_TIMEOUT_SECONDS,
            )
        ],
    )
    result = orchestrator.execute_plan(plan)
    assert custom_runner.calls, "Custom agent should have been executed"
    assert any(item.source == "agent:stub" for item in result.evidence.items)


def test_run_pipeline_blocks_on_audit_failure() -> None:
    orchestrator = WorkflowOrchestrator(
        db_tool=StubDatabaseTool(
            [EvidenceItem(id="db-1", source="db", content="Snippet", score=1.0)]
        ),
        responder=Responder(client=StubResponderClient()),
        audit_agent=StubAuditAgent(passed=False),
    )
    response, result = orchestrator.run_pipeline("hello", build_plan())

    assert result.report.successful is False
    assert any(
        event.task_id == "audit_gate" and event.status == TaskStatus.FAILED
        for event in result.report.events
    )
    assert response.answer.startswith("Audit checks flagged issues")
    assert result.audit_report is not None
    assert result.audit_report.passed is False
