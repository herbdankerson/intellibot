"""Prefect-backed workflow orchestrator."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable, List, Tuple

from prefect import flow, get_run_logger, task

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
from .approver import ApprovalResult, Approver, AutoApprover

LOGGER = logging.getLogger(__name__)


@dataclass
class ToolSuite:
    """Container bundling the available tools."""

    db: DatabaseTool
    graph: GraphTool
    web: WebTool


@dataclass
class FlowRuntime:
    """Runtime dependencies shared across Prefect tasks."""

    tools: ToolSuite
    responder: Responder
    approver: Approver


@dataclass
class FlowOutput:
    """Return type for the Prefect flow."""

    response: AgentResponse | None
    result: ExecutionResult


_RUNTIME: FlowRuntime | None = None


@contextmanager
def workflow_runtime(tools: ToolSuite, responder: Responder, approver: Approver):
    global _RUNTIME
    previous = _RUNTIME
    _RUNTIME = FlowRuntime(tools=tools, responder=responder, approver=approver)
    try:
        yield
    finally:
        _RUNTIME = previous


def _get_runtime() -> FlowRuntime:
    if _RUNTIME is None:
        raise RuntimeError("Workflow runtime not initialized")
    return _RUNTIME


@task(name="approve-plan", persist_result=False)
def _approve_plan(plan: Plan) -> ApprovalResult:
    runtime = _get_runtime()
    return runtime.approver.approve_plan(plan)


@task(name="approve-task", persist_result=False)
def _approve_task(plan_task: PlanTask) -> ApprovalResult:
    runtime = _get_runtime()
    return runtime.approver.approve_task(plan_task)


@task(name="execute-task", persist_result=False)
def _execute_task(plan_task: PlanTask) -> List[EvidenceItem]:
    runtime = _get_runtime()
    return run_tool(runtime.tools, plan_task)


@task(name="assemble-evidence", persist_result=False)
def _assemble_evidence_task(items: List[EvidenceItem]) -> EvidencePack:
    return assemble_evidence(items)


@task(name="generate-response", persist_result=False)
def _generate_response_task(message: str, pack: EvidencePack) -> AgentResponse:
    runtime = _get_runtime()
    return runtime.responder.respond(message, pack)


@task(name="approve-response", persist_result=False)
def _approve_response_task(response: AgentResponse) -> ApprovalResult:
    runtime = _get_runtime()
    return runtime.approver.approve_response(response)


def _resolve_task_result(value):
    return value.result() if hasattr(value, "result") else value


@flow(name="agentic-workflow")
def _agentic_workflow_flow(message: str, plan: Plan, deliver_response: bool) -> FlowOutput:
    runtime = _get_runtime()
    logger = get_run_logger()
    report = ExecutionReport(run_id=generate_run_id())

    plan_decision = _resolve_task_result(_approve_plan(plan))
    if not plan_decision.approved:
        reason = plan_decision.reason or "Plan rejected"
        report.successful = False
        report.notes.append(f"Plan rejected: {reason}")
        report.record(
            ExecutionEvent(task_id="plan", status=TaskStatus.FAILED, message=reason)
        )
        empty_pack = EvidencePack()
        return FlowOutput(
            response=None,
            result=ExecutionResult(evidence=empty_pack, report=report),
        )

    collected: List[EvidenceItem] = []
    for plan_task in plan.tasks:
        if plan_task.requires_approval:
            decision = _resolve_task_result(_approve_task(plan_task))
            if not decision.approved:
                note = decision.reason or "Task rejected"
                report.record(
                    ExecutionEvent(
                        task_id=plan_task.id,
                        status=TaskStatus.FAILED,
                        message=f"Task rejected: {note}",
                    )
                )
                continue
        report.record(
            ExecutionEvent(
                task_id=plan_task.id,
                status=TaskStatus.RUNNING,
                message=f"tool={plan_task.tool}",
            )
        )
        try:
            outputs = _resolve_task_result(_execute_task(plan_task))
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(
                "Tool execution failed",
                extra={"task_id": plan_task.id, "tool": plan_task.tool},
            )
            report.record(
                ExecutionEvent(
                    task_id=plan_task.id,
                    status=TaskStatus.FAILED,
                    message=str(exc),
                )
            )
            continue
        collected.extend(outputs)
        report.record(
            ExecutionEvent(
                task_id=plan_task.id,
                status=TaskStatus.COMPLETED,
                message=f"items={len(outputs)}",
            )
        )

    evidence_pack = _resolve_task_result(_assemble_evidence_task(collected))
    result = ExecutionResult(evidence=evidence_pack, report=report)

    if not deliver_response:
        return FlowOutput(response=None, result=result)

    response = _resolve_task_result(_generate_response_task(message, evidence_pack))
    approval = _resolve_task_result(_approve_response_task(response))
    if not approval.approved:
        reason = approval.reason or "Response rejected by approver."
        note = f"Response rejected: {reason}"
        report.record(
            ExecutionEvent(
                task_id="final_response",
                status=TaskStatus.FAILED,
                message=note,
            )
        )
        report.successful = False
        report.notes.append(note)
        fallback = AgentResponse(
            answer="Final response requires revision before delivery.",
            citations=[],
            confidence=0.0,
            unresolved_questions=[reason],
        )
        return FlowOutput(response=fallback, result=result)

    report.record(
        ExecutionEvent(
            task_id="final_response",
            status=TaskStatus.COMPLETED,
            message="Response approved",
        )
    )
    report.notes.append("Response approved by approver.")
    return FlowOutput(response=response, result=result)


def run_tool(tool_suite: ToolSuite, plan_task: PlanTask) -> List[EvidenceItem]:
    if plan_task.tool == "db_search":
        return tool_suite.db.search(
            plan_task.description, limit=policies.MAX_EVIDENCE_ITEMS
        )
    if plan_task.tool == "graph_search":
        return tool_suite.graph.search(plan_task.description)
    if plan_task.tool == "web_search":
        return tool_suite.web.search(plan_task.description)
    LOGGER.warning("Unsupported tool requested", extra={"tool": plan_task.tool})
    return []


def assemble_evidence(items: Iterable[EvidenceItem]) -> EvidencePack:
    limited = policies.clamp_evidence(items)
    pack = EvidencePack(items=list(limited), summary=summarize_items(limited))
    if policies.within_token_budget(pack):
        return pack
    truncated_items = list(pack.items)
    truncated = False
    while truncated_items and (
        sum(item.token_estimate() for item in truncated_items)
        > policies.EVIDENCE_TOKEN_BUDGET
    ):
        truncated = True
        truncated_items.pop()
    return EvidencePack(
        items=truncated_items,
        summary=summarize_items(truncated_items),
        truncated=truncated,
    )


def summarize_items(items: Iterable[EvidenceItem]) -> str:
    snippets = [item.content for item in items]
    if not snippets:
        return ""
    return "\n".join(snippets[:2])


class WorkflowOrchestrator:
    """Execute plans and collect evidence using Prefect flows."""

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

    def execute_plan(self, plan: Plan, *, message: str | None = None) -> ExecutionResult:
        with workflow_runtime(self.tools, self.responder, self.approver):
            output = _agentic_workflow_flow(
                message=message or "Plan execution",
                plan=plan,
                deliver_response=False,
            )
        return output.result

    def run_pipeline(self, message: str, plan: Plan) -> Tuple[AgentResponse, ExecutionResult]:
        with workflow_runtime(self.tools, self.responder, self.approver):
            output = _agentic_workflow_flow(
                message=message,
                plan=plan,
                deliver_response=True,
            )
        response = output.response
        if response is None:  # pragma: no cover - defensive fallback
            response = AgentResponse(
                answer="Final response requires revision before delivery.",
                citations=[],
                confidence=0.0,
                unresolved_questions=["Responder did not return a response."],
            )
        return response, output.result


__all__ = [
    "ToolSuite",
    "WorkflowOrchestrator",
    "assemble_evidence",
    "run_tool",
]
