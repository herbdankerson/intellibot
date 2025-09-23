"""Workflow orchestrator that executes planner tasks and assembles evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Tuple

from ..response.responder import Responder
from ..schemas import (
    AgentResponse,
    EvidenceItem,
    EvidencePack,
    ExecutionEvent,
    ExecutionReport,
    ExecutionResult,
    Plan,
    PlanTask,
    TaskStatus,
)
from ..tools.db_tools import DatabaseTool
from ..tools.graph_tools import GraphTool
from ..tools.web_tools import WebTool
from ..util.tracing import generate_run_id
from . import policies
from .approver import Approver, AutoApprover


@dataclass
class ToolSuite:
    """Container bundling the available tools."""

    db: DatabaseTool
    graph: GraphTool
    web: WebTool


class WorkflowOrchestrator:
    """Execute plans and collect evidence using registered tool adapters."""

    def __init__(
        self,
        db_tool: DatabaseTool | None = None,
        graph_tool: GraphTool | None = None,
        web_tool: WebTool | None = None,
        responder: Responder | None = None,
        approver: Approver | None = None,
    ) -> None:
        self.tools = ToolSuite(
            db=db_tool or DatabaseTool(),
            graph=graph_tool or GraphTool(),
            web=web_tool or WebTool(),
        )
        self.responder = responder or Responder()
        self.approver = approver or AutoApprover()

    @property
    def db_tool(self) -> DatabaseTool:
        return self.tools.db

    def execute_plan(self, plan: Plan) -> ExecutionResult:
        """Execute a plan and return the resulting evidence and report."""

        run_id = generate_run_id()
        report = ExecutionReport(run_id=run_id)
        plan_approval = self.approver.approve_plan(plan)
        if not plan_approval.approved:
            report.successful = False
            report.notes.append(f"Plan rejected: {plan_approval.reason}")
            return ExecutionResult(evidence=EvidencePack(), report=report)

        collected: List[EvidenceItem] = []
        for task in plan.tasks:
            if task.requires_approval:
                decision = self.approver.approve_task(task)
                if not decision.approved:
                    report.record(
                        ExecutionEvent(
                            task_id=task.id,
                            status=TaskStatus.FAILED,
                            message=f"Task rejected: {decision.reason}",
                        )
                    )
                    continue

            report.record(
                ExecutionEvent(
                    task_id=task.id,
                    status=TaskStatus.RUNNING,
                    message=f"tool={task.tool}",
                )
            )
            try:
                outputs = self._run_task(task)
            except Exception as exc:  # pragma: no cover - defensive branch
                report.record(
                    ExecutionEvent(
                        task_id=task.id,
                        status=TaskStatus.FAILED,
                        message=str(exc),
                    )
                )
                continue

            collected.extend(outputs)
            report.record(
                ExecutionEvent(
                    task_id=task.id,
                    status=TaskStatus.COMPLETED,
                    message=f"items={len(outputs)}",
                )
            )

        evidence = self._assemble_evidence(collected)
        return ExecutionResult(evidence=evidence, report=report)

    def run_pipeline(self, message: str, plan: Plan) -> Tuple[AgentResponse, ExecutionResult]:
        """Execute a plan and produce the final response payload."""

        result = self.execute_plan(plan)
        response = self.responder.respond(message, result.evidence)
        approval = self.approver.approve_response(response)
        if not approval.approved:
            reason = approval.reason or "Response rejected by approver."
            note = f"Response rejected: {reason}"
            result.report.record(
                ExecutionEvent(
                    task_id="final_response",
                    status=TaskStatus.FAILED,
                    message=note,
                )
            )
            result.report.notes.append(note)
            return (
                AgentResponse(
                    answer="Final response requires revision before delivery.",
                    citations=[],
                    confidence=0.0,
                    unresolved_questions=[reason],
                ),
                result,
            )

        result.report.record(
            ExecutionEvent(
                task_id="final_response",
                status=TaskStatus.COMPLETED,
                message="Response approved",
            )
        )
        result.report.notes.append("Response approved by approver.")
        return response, result

    def _run_task(self, task: PlanTask) -> List[EvidenceItem]:
        if task.tool == "db_search":
            return self.tools.db.search(task.description, limit=policies.MAX_EVIDENCE_ITEMS)
        if task.tool == "graph_search":
            return self.tools.graph.search(task.description)
        if task.tool == "web_search":
            return self.tools.web.search(task.description)
        return []

    def _assemble_evidence(self, items: Iterable[EvidenceItem]) -> EvidencePack:
        limited = policies.clamp_evidence(items)
        pack = EvidencePack(items=list(limited), summary=self._summarize_items(limited))
        if policies.within_token_budget(pack):
            return pack
        truncated_items = list(pack.items)
        truncated = False
        while truncated_items and sum(item.token_estimate() for item in truncated_items) > policies.EVIDENCE_TOKEN_BUDGET:
            truncated = True
            truncated_items.pop()
        return EvidencePack(
            items=truncated_items,
            summary=self._summarize_items(truncated_items),
            truncated=truncated,
        )

    def _summarize_items(self, items: Iterable[EvidenceItem]) -> str:
        snippets = [item.content for item in items]
        if not snippets:
            return ""
        return "\n".join(snippets[:2])


__all__ = ["ToolSuite", "WorkflowOrchestrator"]
