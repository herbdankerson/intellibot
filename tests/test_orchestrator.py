"""Tests for the workflow orchestrator."""

import json
from unittest.mock import patch

from src.my_agentic_chatbot.schemas import (
    AgentResponse,
    AuditFinding,
    AuditReport,
    EvidenceItem,
    Finding,
    Plan,
    PlanTask,
    Requirement,
    TaskStatus,
)
from src.my_agentic_chatbot.tools import ToolOutcome
from src.my_agentic_chatbot.workflows.approver import ApprovalResult, Approver
from src.my_agentic_chatbot.workflows.orchestrator import WorkflowOrchestrator
from src.my_agentic_chatbot.response.responder import Responder
from src.my_agentic_chatbot.workflows import policies


class StubDatabaseTool:
    def __init__(self, items: list[EvidenceItem]) -> None:
        self.items = items
        self.queries: list[str] = []

    def execute(
        self,
        task: PlanTask,
        requirement: Requirement,
        *,
        limit: int | None = None,
        scope: list[str] | None = None,
        overrides: dict | None = None,
    ) -> ToolOutcome:
        _ = scope, overrides
        query = task.inputs.get("query") if isinstance(task.inputs, dict) else None
        self.queries.append(query or task.description)
        evidence = self.items[: limit or len(self.items)]
        findings = [
            Finding(
                id=f"finding-{index + 1}",
                requirement_id=requirement.id,
                key=requirement.question,
                value=item.content,
                confidence=0.7,
                evidence_ids=[item.id],
                metadata={"source": item.source},
            )
            for index, item in enumerate(evidence)
        ]
        return ToolOutcome(evidence=evidence, findings=findings)


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
    requirement = Requirement(
        id="req-1",
        question="Describe the evidence pack process",
        priority=1,
        quality_bar="At least one snippet",
        stop_when_satisfied=True,
        metadata={},
    )
    task = PlanTask(
        id="db-1",
        requirement_id=requirement.id,
        description="Retrieve evidence",
        tool="db_search",
        priority=1,
        budget_tokens=policies.DEFAULT_DB_BUDGET_TOKENS,
        timeout_seconds=policies.DEFAULT_DB_TIMEOUT_SECONDS,
    )
    return Plan(
        problem_spec=requirement.question,
        acceptance_criteria=["Answer references collected evidence."],
        requirements=[requirement],
        tasks=[task],
        findings=[],
        open_questions=[],
        stop_conditions=["Acceptance criteria met"],
    )


def test_orchestrator_executes_plan() -> None:
    evidence = [
        EvidenceItem(
            id="db-1",
            source="https://example.com/source-1",
            content="Snippet",
            score=1.0,
        ),
        EvidenceItem(
            id="db-2",
            source="https://example.net/source-2",
            content="Snippet 2",
            score=0.5,
        ),
    ]
    orchestrator = WorkflowOrchestrator(
        db_tool=StubDatabaseTool(evidence),
        responder=Responder(client=StubResponderClient()),
        audit_agent=StubAuditAgent(),
    )
    plan = build_plan()
    with patch(
        "src.my_agentic_chatbot.planner.main_planner.revise_plan_with_evidence",
        side_effect=lambda plan, **_: plan,
    ):
        result = orchestrator.execute_plan(plan)
    assert result.evidence.items, "Orchestrator should collect evidence"
    assert result.report.events, "Execution report should track events"
    assert result.report.successful is True
    statuses = {event.status for event in result.report.events}
    assert TaskStatus.PENDING not in statuses


def test_run_pipeline_rejects_final_response() -> None:
    orchestrator = WorkflowOrchestrator(
        db_tool=StubDatabaseTool(
            [
                EvidenceItem(
                    id="db-1",
                    source="https://example.com/source-1",
                    content="Snippet",
                    score=1.0,
                ),
                EvidenceItem(
                    id="db-2",
                    source="https://example.net/source-2",
                    content="Snippet 2",
                    score=0.6,
                ),
            ]
        ),
        responder=Responder(client=StubResponderClient()),
        approver=RejectingApprover(),
        audit_agent=StubAuditAgent(),
    )
    with patch(
        "src.my_agentic_chatbot.planner.main_planner.revise_plan_with_evidence",
        side_effect=lambda plan, **_: plan,
    ):
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

    def execute(
        self,
        task: PlanTask,
        requirement: Requirement,
        *,
        scope: list[str] | None = None,
        overrides: dict | None = None,
    ) -> ToolOutcome:
        _ = scope, overrides
        self.calls.append(task)
        evidence = [
            EvidenceItem(
                id="agent-1",
                source="agent:stub:1",
                content="Agent produced evidence",
                score=0.8,
            ),
            EvidenceItem(
                id="agent-2",
                source="agent:stub:2",
                content="Agent produced more evidence",
                score=0.75,
            ),
        ]
        findings = [
            Finding(
                id="agent-finding-1",
                requirement_id=requirement.id,
                key=requirement.question,
                value=evidence[0].content,
                confidence=0.8,
                evidence_ids=[evidence[0].id],
                metadata={"source": evidence[0].source},
            ),
            Finding(
                id="agent-finding-2",
                requirement_id=requirement.id,
                key=requirement.question,
                value=evidence[1].content,
                confidence=0.75,
                evidence_ids=[evidence[1].id],
                metadata={"source": evidence[1].source},
            ),
        ]
        return ToolOutcome(evidence=evidence, findings=findings)


def test_orchestrator_executes_custom_agent() -> None:
    custom_runner = StubAgentRunner()
    orchestrator = WorkflowOrchestrator(
        db_tool=StubDatabaseTool([]),
        responder=Responder(client=StubResponderClient()),
        custom_agents={"agent-demo": custom_runner},
        audit_agent=StubAuditAgent(),
    )
    plan = Plan(
        problem_spec="Use custom agent",
        acceptance_criteria=["Incorporate agent output"],
        requirements=[
            Requirement(
                id="req-1",
                question="Collect agent evidence",
                priority=1,
                quality_bar="At least one snippet",
                stop_when_satisfied=True,
                metadata={},
            )
        ],
        tasks=[
            PlanTask(
                id="agent-demo",
                requirement_id="req-1",
                description="Delegate to custom agent",
                tool="agent-demo",
                priority=1,
                budget_tokens=policies.DEFAULT_DB_BUDGET_TOKENS,
                timeout_seconds=policies.DEFAULT_TOOL_TIMEOUT_SECONDS,
            )
        ],
        findings=[],
        open_questions=[],
        stop_conditions=["Acceptance criteria met"],
    )
    with patch(
        "src.my_agentic_chatbot.planner.main_planner.revise_plan_with_evidence",
        side_effect=lambda plan, **_: plan,
    ):
        result = orchestrator.execute_plan(plan)
    assert custom_runner.calls, "Custom agent should have been executed"
    assert any(item.source.startswith("agent:stub") for item in result.evidence.items)


def test_run_pipeline_blocks_on_audit_failure() -> None:
    orchestrator = WorkflowOrchestrator(
        db_tool=StubDatabaseTool(
            [
                EvidenceItem(
                    id="db-1",
                    source="https://example.com/source-1",
                    content="Snippet",
                    score=1.0,
                ),
                EvidenceItem(
                    id="db-2",
                    source="https://example.net/source-2",
                    content="Snippet 2",
                    score=0.8,
                ),
            ]
        ),
        responder=Responder(client=StubResponderClient()),
        audit_agent=StubAuditAgent(passed=False),
    )
    with patch(
        "src.my_agentic_chatbot.planner.main_planner.revise_plan_with_evidence",
        side_effect=lambda plan, **_: plan,
    ):
        response, result = orchestrator.run_pipeline("hello", build_plan())

    assert result.report.successful is False
    assert any(
        event.task_id == "audit_gate" and event.status == TaskStatus.FAILED
        for event in result.report.events
    )
    answer_lower = response.answer.lower()
    assert "audit failed" in answer_lower and "flagged" in answer_lower
    assert result.audit_report is not None
    assert result.audit_report.passed is False
