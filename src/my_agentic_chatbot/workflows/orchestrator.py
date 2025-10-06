"""Prefect-backed workflow orchestrator."""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

try:  # pragma: no cover - exercised via integration tests when Prefect is available
    from prefect import flow, get_run_logger, task
    from prefect.settings import (
        PREFECT_API_URL,
        PREFECT_SERVER_EPHEMERAL_ENABLED,
        PREFECT_SERVER_EPHEMERAL_STARTUP_TIMEOUT_SECONDS,
        temporary_settings,
    )
except Exception:  # pragma: no cover - fallback for unit tests/local dev
    def flow(*_args, **_kwargs):  # type: ignore[misc]
        def decorator(fn):
            return fn

        return decorator

    def task(*_args, **_kwargs):  # type: ignore[misc]
        def decorator(fn):
            return fn

        return decorator

    def get_run_logger():
        return logging.getLogger("prefect-fallback")

    def temporary_settings(_overrides):  # type: ignore[override]
        return nullcontext()

    PREFECT_API_URL = "PREFECT_API_URL"
    PREFECT_SERVER_EPHEMERAL_ENABLED = "PREFECT_SERVER_EPHEMERAL_ENABLED"
    PREFECT_SERVER_EPHEMERAL_STARTUP_TIMEOUT_SECONDS = (
        "PREFECT_SERVER_EPHEMERAL_STARTUP_TIMEOUT_SECONDS"
    )

from ..agents import get_agent_config, iter_custom_agent_descriptors
from ..agents.runtime import CustomAgentRunner
from ..config import get_settings
from ..response.responder import Responder
from ..schemas import (
    AgentResponse,
    AuditReport,
    EvidenceItem,
    EvidencePack,
    ExecutionEvent,
    ExecutionReport,
    ExecutionResult,
    Plan,
    TaskStatus,
)
from ..tools.db_tools import DatabaseTool
from ..tools.graph_tools import GraphTool
from ..tools.web_tools import WebTool
from ..run_logging import AgentRunLogger
from ..util.text import (
    clip_to_token_budget,
    deduplicate_items,
    reciprocal_rank_fuse,
    summarize_evidence,
)
from ..util.tracing import generate_run_id
from . import policies
from .approver import ApprovalResult, Approver, AutoApprover
from .audit import AuditAgent
from .workflow_designer import WorkflowDesigner, WorkflowNode, WorkflowNodeType

LOGGER = logging.getLogger(__name__)


def _safe_task_inputs(raw: object) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    safe: Dict[str, Any] = {}
    for key, value in raw.items():
        try:
            json.dumps(value)
            safe[str(key)] = value
        except TypeError:
            safe[str(key)] = str(value)
    return safe


@dataclass
class ToolSuite:
    """Container bundling the available tools."""

    db: DatabaseTool
    graph: GraphTool
    web: WebTool
    agents: Dict[str, CustomAgentRunner] = field(default_factory=dict)


@dataclass
class FlowRuntime:
    """Runtime dependencies shared across Prefect tasks."""

    tools: ToolSuite
    responder: Responder
    approver: Approver
    designer: WorkflowDesigner
    audit_agent: Optional[AuditAgent] = None
    logger: Optional[AgentRunLogger] = None


@dataclass
class FlowOutput:
    """Return type for the Prefect flow."""

    response: AgentResponse | None
    result: ExecutionResult
    audit_report: AuditReport | None = None


_RUNTIME: FlowRuntime | None = None


@contextmanager
def workflow_runtime(
    tools: ToolSuite,
    responder: Responder,
    approver: Approver,
    designer: WorkflowDesigner,
    audit_agent: Optional[AuditAgent],
    logger: Optional[AgentRunLogger],
):
    global _RUNTIME
    previous = _RUNTIME
    _RUNTIME = FlowRuntime(
        tools=tools,
        responder=responder,
        approver=approver,
        designer=designer,
        audit_agent=audit_agent,
        logger=logger,
    )
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
def _approve_task(node: WorkflowNode) -> ApprovalResult:
    runtime = _get_runtime()
    return runtime.approver.approve_task(node.task)


@task(name="execute-task", persist_result=False)
def _execute_task(node: WorkflowNode) -> List[EvidenceItem]:
    runtime = _get_runtime()
    return run_tool(runtime.tools, node)


@task(name="assemble-evidence", persist_result=False)
def _assemble_evidence_task(items: Sequence[EvidenceItem]) -> EvidencePack:
    return assemble_evidence(items)


@task(name="generate-response", persist_result=False)
def _generate_response_task(message: str, pack: EvidencePack) -> AgentResponse:
    runtime = _get_runtime()
    return runtime.responder.respond(message, pack)


@task(name="approve-evidence", persist_result=False)
def _approve_evidence_task(pack: EvidencePack) -> ApprovalResult:
    runtime = _get_runtime()
    return runtime.approver.approve_evidence(pack)


@task(name="approve-response", persist_result=False)
def _approve_response_task(response: AgentResponse) -> ApprovalResult:
    runtime = _get_runtime()
    return runtime.approver.approve_response(response)


@task(name="audit-response", persist_result=False)
def _audit_response_task(
    message: str,
    plan: Plan,
    pack: EvidencePack,
    response: AgentResponse | None,
) -> AuditReport | None:
    runtime = _get_runtime()
    if runtime.audit_agent is None or response is None:
        if runtime.logger is not None:
            runtime.logger.log_event(
                "audit_skipped",
                {"reason": "audit_agent_unavailable"},
                status="skipped",
            )
        return None
    audit_report = runtime.audit_agent.evaluate(
        question=message,
        plan=plan,
        evidence=pack,
        response=response,
    )
    if runtime.logger is not None:
        runtime.logger.log_audit(audit_report)
    return audit_report


def _resolve_task_result(value):
    return value.result() if hasattr(value, "result") else value


@flow(name="agentic-workflow")
def _agentic_workflow_flow(message: str, plan: Plan, deliver_response: bool) -> FlowOutput:
    runtime = _get_runtime()
    logger = get_run_logger()
    run_logger = runtime.logger
    if run_logger is not None:
        run_logger.log_event(
            "workflow_started",
            {"deliver_response": deliver_response},
        )
    report = ExecutionReport(run_id=generate_run_id())

    plan_decision = _resolve_task_result(_approve_plan(plan))
    if run_logger is not None:
        run_logger.log_event(
            "plan_approval",
            {
                "approved": plan_decision.approved,
                "reason": plan_decision.reason,
            },
            status="approved" if plan_decision.approved else "rejected",
        )
    if not plan_decision.approved:
        reason = plan_decision.reason or "Plan rejected"
        report.successful = False
        report.notes.append(f"Plan rejected: {reason}")
        report.record(
            ExecutionEvent(task_id="plan", status=TaskStatus.FAILED, message=reason)
        )
        if run_logger is not None:
            run_logger.log_event(
                "workflow_aborted",
                {"reason": reason},
                status="plan_rejected",
            )
        empty_pack = EvidencePack()
        return FlowOutput(
            response=None,
            result=ExecutionResult(evidence=empty_pack, report=report),
        )

    try:
        workflow_graph = runtime.designer.build_graph(plan)
        if run_logger is not None:
            run_logger.log_event(
                "workflow_graph_built",
                {
                    "nodes": [
                        {
                            "id": node.id,
                            "tool": node.task.tool,
                            "dependencies": node.dependencies,
                            "max_attempts": node.max_attempts,
                        }
                        for node in workflow_graph.nodes.values()
                    ]
                },
            )
    except ValueError as exc:
        logger.exception("Workflow designer failed to build graph")
        report.successful = False
        report.notes.append(str(exc))
        report.record(
            ExecutionEvent(
                task_id="workflow",
                status=TaskStatus.FAILED,
                message="Workflow designer failed",
            )
        )
        if run_logger is not None:
            run_logger.log_event(
                "workflow_aborted",
                {"reason": "workflow_designer_error", "details": str(exc)},
                status="designer_error",
            )
        empty_pack = EvidencePack()
        return FlowOutput(
            response=None,
            result=ExecutionResult(evidence=empty_pack, report=report),
        )

    collected: List[EvidenceItem] = []
    status_map: Dict[str, TaskStatus] = {}
    completed: Set[str] = set()
    failed: Set[str] = set()
    skipped: Set[str] = set()

    for node in workflow_graph.ordered():
        dependency_messages: List[str] = []
        for dependency in node.dependencies:
            dep_status = status_map.get(dependency)
            if dep_status is None:
                dependency_messages.append(f"{dependency} pending")
                continue
            allowed_statuses = node.dependency_statuses(dependency)
            if dep_status not in allowed_statuses:
                allowed_labels = ", ".join(sorted(status.value for status in allowed_statuses))
                dependency_messages.append(
                    f"{dependency} status {dep_status.value} not in [{allowed_labels}]"
                )
        if dependency_messages:
            note = "; ".join(dependency_messages)
            report.record(
                ExecutionEvent(
                    task_id=node.id,
                    status=TaskStatus.SKIPPED,
                    message=note,
                )
            )
            skipped.add(node.id)
            status_map[node.id] = TaskStatus.SKIPPED
            if run_logger is not None:
                run_logger.log_event(
                    "task_skipped",
                    {"reason": note},
                    task_id=node.id,
                    tool=node.task.tool,
                    status="skipped",
                )
            continue

        if not node.should_run(status_map):
            report.record(
                ExecutionEvent(
                    task_id=node.id,
                    status=TaskStatus.SKIPPED,
                    message="Branch conditions not satisfied",
                )
            )
            skipped.add(node.id)
            status_map[node.id] = TaskStatus.SKIPPED
            if run_logger is not None:
                run_logger.log_event(
                    "task_skipped",
                    {"reason": "branch_conditions"},
                    task_id=node.id,
                    tool=node.task.tool,
                    status="skipped",
                )
            continue

        policy_decision = policies.enforce_task_limits(node.task)
        if not policy_decision.allowed:
            reason = policy_decision.reason or "policy rejection"
            report.record(
                ExecutionEvent(
                    task_id=node.id,
                    status=TaskStatus.FAILED,
                    message=f"Policy rejected task: {reason}",
                )
            )
            report.notes.append(f"Policy rejected {node.id}: {reason}")
            failed.add(node.id)
            status_map[node.id] = TaskStatus.FAILED
            if run_logger is not None:
                run_logger.log_tool_result(
                    task_id=node.id,
                    tool=node.task.tool,
                    status="policy_rejected",
                    inputs=_safe_task_inputs(node.task.inputs),
                    error=reason,
                )
            continue

        if node.task.requires_approval:
            decision = _resolve_task_result(_approve_task(node))
            if not decision.approved:
                note = decision.reason or "Task rejected"
                report.record(
                    ExecutionEvent(
                        task_id=node.id,
                        status=TaskStatus.FAILED,
                        message=f"Task rejected: {note}",
                    )
                )
                failed.add(node.id)
                status_map[node.id] = TaskStatus.FAILED
                if run_logger is not None:
                    run_logger.log_event(
                        "task_rejected",
                        {"reason": note},
                        task_id=node.id,
                        tool=node.task.tool,
                        status="rejected",
                    )
                continue

        report.record(
            ExecutionEvent(
                task_id=node.id,
                status=TaskStatus.RUNNING,
                message=f"tool={node.task.tool}, attempts={node.max_attempts}",
            )
        )
        if run_logger is not None:
            run_logger.log_event(
                "task_started",
                {
                    "attempts": node.max_attempts,
                    "inputs": _safe_task_inputs(node.task.inputs),
                },
                task_id=node.id,
                tool=node.task.tool,
                status="running",
            )
        try:
            outputs = _execute_with_retries(node)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(
                "Tool execution failed",
                extra={"task_id": node.id, "tool": node.task.tool},
            )
            report.record(
                ExecutionEvent(
                    task_id=node.id,
                    status=TaskStatus.FAILED,
                    message=str(exc),
                )
            )
            failed.add(node.id)
            status_map[node.id] = TaskStatus.FAILED
            if run_logger is not None:
                run_logger.log_tool_result(
                    task_id=node.id,
                    tool=node.task.tool,
                    status="failed",
                    inputs=_safe_task_inputs(node.task.inputs),
                    error=str(exc),
                )
            continue
        collected.extend(outputs)
        completed.add(node.id)
        status_map[node.id] = TaskStatus.COMPLETED
        report.record(
            ExecutionEvent(
                task_id=node.id,
                status=TaskStatus.COMPLETED,
                message=f"items={len(outputs)}",
            )
        )
        if run_logger is not None:
            run_logger.log_tool_result(
                task_id=node.id,
                tool=node.task.tool,
                status="completed",
                inputs=_safe_task_inputs(node.task.inputs),
                outputs=outputs,
            )

    if skipped:
        report.notes.append("Skipped tasks: " + ", ".join(sorted(skipped)))

    evidence_pack = _resolve_task_result(_assemble_evidence_task(collected))
    if run_logger is not None:
        run_logger.log_evidence(evidence_pack)
    evidence_decision = _resolve_task_result(_approve_evidence_task(evidence_pack))
    if run_logger is not None:
        run_logger.log_event(
            "evidence_approval",
            {
                "approved": evidence_decision.approved,
                "reason": evidence_decision.reason,
            },
            status="approved" if evidence_decision.approved else "rejected",
        )
    if not evidence_decision.approved:
        reason = evidence_decision.reason or "Evidence pack rejected"
        report.record(
            ExecutionEvent(
                task_id="evidence_gate",
                status=TaskStatus.FAILED,
                message=reason,
            )
        )
        report.notes.append(f"Evidence rejected: {reason}")
        report.successful = False
        if run_logger is not None:
            run_logger.log_event(
                "workflow_aborted",
                {"reason": reason},
                status="evidence_rejected",
            )
        return FlowOutput(
            response=None,
            result=ExecutionResult(evidence=evidence_pack, report=report),
        )

    if failed:
        report.successful = False
    audit_report: AuditReport | None = None

    if not deliver_response:
        result = ExecutionResult(evidence=evidence_pack, report=report, audit_report=None)
        return FlowOutput(response=None, result=result)

    response = _resolve_task_result(_generate_response_task(message, evidence_pack))
    if run_logger is not None and response is not None:
        run_logger.log_response(response)
    audit_report = _resolve_task_result(
        _audit_response_task(message, plan, evidence_pack, response)
    )
    if audit_report and not audit_report.passed:
        summary = audit_report.summary or "Audit checks failed"
        report.record(
            ExecutionEvent(
                task_id="audit_gate",
                status=TaskStatus.FAILED,
                message=summary,
            )
        )
        report.notes.append(summary)
        report.successful = False
        if run_logger is not None:
            run_logger.log_event(
                "workflow_aborted",
                {"reason": summary},
                status="audit_failed",
            )
        blocking_finding = next(
            (
                finding
                for finding in audit_report.findings
                if finding.severity == "error"
            ),
            None,
        )
        message_reason = blocking_finding.message if blocking_finding else summary
        fallback = AgentResponse(
            answer="Audit checks flagged issues requiring human review.",
            citations=response.citations if response else [],
            confidence=0.0,
            unresolved_questions=[message_reason],
        )
        result = ExecutionResult(
            evidence=evidence_pack,
            report=report,
            audit_report=audit_report,
        )
        return FlowOutput(response=fallback, result=result, audit_report=audit_report)

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
        if run_logger is not None:
            run_logger.log_event(
                "workflow_aborted",
                {"reason": reason},
                status="response_rejected",
            )
        fallback = AgentResponse(
            answer="Final response requires revision before delivery.",
            citations=[],
            confidence=0.0,
            unresolved_questions=[reason],
        )
        result = ExecutionResult(
            evidence=evidence_pack,
            report=report,
            audit_report=audit_report,
        )
        return FlowOutput(response=fallback, result=result, audit_report=audit_report)

    report.record(
        ExecutionEvent(
            task_id="final_response",
            status=TaskStatus.COMPLETED,
            message="Response approved",
        )
    )
    report.notes.append("Response approved by approver.")
    result = ExecutionResult(
        evidence=evidence_pack,
        report=report,
        audit_report=audit_report,
    )
    return FlowOutput(response=response, result=result, audit_report=audit_report)


def run_tool(tool_suite: ToolSuite, node: WorkflowNode) -> List[EvidenceItem]:
    task = node.task
    if node.node_type == WorkflowNodeType.DB_QUERY:
        return tool_suite.db.execute(task, limit=policies.MAX_EVIDENCE_ITEMS)
    if node.node_type == WorkflowNodeType.GRAPH_QUERY:
        return tool_suite.graph.execute(task)
    if node.node_type == WorkflowNodeType.WEB_FETCH:
        return tool_suite.web.execute(task, limit=policies.MAX_WEB_RESULTS)
    if node.node_type == WorkflowNodeType.CUSTOM_AGENT:
        runner = tool_suite.agents.get(task.tool)
        if runner is None:
            LOGGER.warning(
                "Custom agent missing runner",
                extra={"tool": task.tool, "task_id": node.id},
            )
            return []
        return runner.execute(task)
    runner = tool_suite.agents.get(task.tool)
    if runner is not None:
        return runner.execute(task)
    LOGGER.warning("Unsupported workflow node", extra={"node": node.node_type.value})
    return []


def _execute_with_retries(node: WorkflowNode) -> List[EvidenceItem]:
    """Execute a workflow node with retry semantics."""

    attempts = max(1, node.max_attempts)
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _resolve_task_result(_execute_task(node))
        except Exception as exc:  # pragma: no cover - defensive guard
            last_exc = exc
            if attempt >= attempts:
                break
            LOGGER.warning(
                "Retrying task after failure",
                extra={
                    "task_id": node.id,
                    "tool": node.task.tool,
                    "attempt": attempt,
                    "max_attempts": attempts,
                },
            )
            if node.retry_delay_seconds > 0:
                time.sleep(node.retry_delay_seconds)
    if last_exc is not None:
        raise last_exc
    return []


def assemble_evidence(items: Iterable[EvidenceItem]) -> EvidencePack:
    deduped = deduplicate_items(items)
    fused = reciprocal_rank_fuse(deduped)
    limited = policies.clamp_evidence(fused)
    ordered_items = list(limited)
    summary = summarize_evidence(ordered_items)
    pack = EvidencePack(items=ordered_items, summary=summary)
    if policies.within_token_budget(pack):
        return pack
    clipped_items, truncated = clip_to_token_budget(
        ordered_items, token_budget=policies.EVIDENCE_TOKEN_BUDGET
    )
    clipped_summary = summarize_evidence(clipped_items)
    if truncated and clipped_summary:
        clipped_summary = f"{clipped_summary}\n(truncated for budget)"
    return EvidencePack(
        items=clipped_items,
        summary=clipped_summary,
        truncated=truncated,
    )


class WorkflowOrchestrator:
    """Execute plans and collect evidence using Prefect flows."""

    def __init__(
        self,
        db_tool: DatabaseTool | None = None,
        graph_tool: GraphTool | None = None,
        web_tool: WebTool | None = None,
        responder: Responder | None = None,
        approver: Approver | None = None,
        designer: WorkflowDesigner | None = None,
        custom_agents: Dict[str, CustomAgentRunner] | None = None,
        audit_agent: AuditAgent | None = None,
    ) -> None:
        self.tools = ToolSuite(
            db=db_tool or DatabaseTool(),
            graph=graph_tool or GraphTool(),
            web=web_tool or WebTool(),
            agents=custom_agents or self._build_custom_agents(),
        )
        self.responder = responder or Responder()
        self.approver = approver or AutoApprover()
        self.designer = designer or WorkflowDesigner()
        if audit_agent is None:
            try:
                audit_agent = AuditAgent()
            except FileNotFoundError:
                LOGGER.warning("Audit agent configuration missing; disabling audit gate")
                audit_agent = None
        self.audit_agent = audit_agent

    def _build_custom_agents(self) -> Dict[str, CustomAgentRunner]:
        runners: Dict[str, CustomAgentRunner] = {}
        for descriptor in iter_custom_agent_descriptors():
            config_name = descriptor.agent_config_name
            if not config_name:
                continue
            try:
                agent_config = get_agent_config(config_name)
            except FileNotFoundError:
                LOGGER.warning(
                    "Missing agent configuration for custom agent",
                    extra={"agent": config_name, "tool": descriptor.tool},
                )
                continue
            runners[descriptor.tool] = CustomAgentRunner(
                descriptor=descriptor,
                agent_config=agent_config,
            )
        return runners

    @property
    def db_tool(self) -> DatabaseTool:
        return self.tools.db

    @contextmanager
    def _prefect_settings_context(self):
        settings = get_settings()
        overrides = {
            PREFECT_SERVER_EPHEMERAL_ENABLED: settings.prefect_server_ephemeral_enabled,
        }
        overrides[
            PREFECT_SERVER_EPHEMERAL_STARTUP_TIMEOUT_SECONDS
        ] = settings.prefect_server_ephemeral_startup_timeout_seconds
        api_url = settings.prefect_api_url
        if api_url:
            overrides[PREFECT_API_URL] = api_url
        context = temporary_settings(overrides) if overrides else nullcontext()
        with context:
            yield

    def execute_plan(
        self,
        plan: Plan,
        *,
        message: str | None = None,
        run_logger: AgentRunLogger | None = None,
    ) -> ExecutionResult:
        with workflow_runtime(
            self.tools,
            self.responder,
            self.approver,
            self.designer,
            self.audit_agent,
            run_logger,
        ):
            with self._prefect_settings_context():
                output = _agentic_workflow_flow(
                    message=message or "Plan execution",
                    plan=plan,
                    deliver_response=False,
                )
        return output.result

    def run_pipeline(
        self,
        message: str,
        plan: Plan,
        *,
        run_logger: AgentRunLogger | None = None,
    ) -> Tuple[AgentResponse, ExecutionResult]:
        with workflow_runtime(
            self.tools,
            self.responder,
            self.approver,
            self.designer,
            self.audit_agent,
            run_logger,
        ):
            with self._prefect_settings_context():
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
