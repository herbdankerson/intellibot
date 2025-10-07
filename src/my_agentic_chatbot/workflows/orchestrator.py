"""Agent Framework-powered workflow orchestrator."""

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from agent_framework import Executor, WorkflowBuilder, WorkflowContext, handler

from ..agents import get_agent_config, iter_custom_agent_descriptors
from ..agents.runtime import CustomAgentRunner
from ..config import get_settings
from ..constants import (
    ACCEPTANCE_BASE_MIN_SOURCES,
    ACCEPTANCE_CONFIDENCE_THRESHOLD,
    ACCEPTANCE_STRICT_MIN_SOURCES,
    MAX_WORKFLOW_ITERATIONS,
)
from ..llm_calls.llm_client import push_run_logger, reset_run_logger
from ..response.responder import Responder
from ..run_logging import AgentRunLogger
from ..schemas import (
    AgentResponse,
    AuditReport,
    EvidenceItem,
    EvidencePack,
    ExecutionEvent,
    ExecutionReport,
    ExecutionResult,
    Finding,
    OpenQuestion,
    Plan,
    PlanTask,
    Requirement,
    TaskStatus,
)
from ..tools import (
    DatabaseTool,
    GraphTool,
    MCPJsonTool,
    SequentialThinkingTool,
    ToolOutcome,
    WebTool,
)
from ..util.text import (
    clip_to_token_budget,
    deduplicate_items,
    reciprocal_rank_fuse,
    summarize_evidence,
)
from ..util.tracing import generate_run_id
from .approver import ApprovalResult, Approver, AutoApprover
from .audit import AuditAgent
from . import policies

LOGGER = logging.getLogger(__name__)


@dataclass
class RequirementEvaluation:
    """Acceptance-aware evaluation metadata for a requirement."""

    requirement_id: str
    min_sources: int
    satisfied: bool = False
    confidence: float = 0.0
    supporting_evidence_ids: List[str] = field(default_factory=list)
    authority_score: float = 0.0
    agreement_score: float = 0.0
    quality_score: float = 0.0
    issues: List[str] = field(default_factory=list)


@dataclass
class WorkflowInput:
    """Initial payload passed into the Agent Framework workflow."""

    user_message: str
    plan: Plan
    deliver_response: bool
    run_logger: AgentRunLogger | None
    run_id: str


@dataclass
class PlanLoopState:
    """Mutable state carried between workflow executors."""

    user_message: str
    plan: Plan
    report: ExecutionReport
    run_logger: AgentRunLogger | None
    deliver_response: bool
    run_id: str = field(default_factory=generate_run_id)
    evidence: List[EvidenceItem] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    open_questions: List[OpenQuestion] = field(default_factory=list)
    requirement_status: Dict[str, RequirementEvaluation] = field(default_factory=dict)
    task_status: Dict[str, TaskStatus] = field(default_factory=dict)
    iteration: int = 0
    max_iterations: int = MAX_WORKFLOW_ITERATIONS
    acceptance_threshold: float = ACCEPTANCE_CONFIDENCE_THRESHOLD
    base_min_sources: int = ACCEPTANCE_BASE_MIN_SOURCES
    strict_min_sources: int = ACCEPTANCE_STRICT_MIN_SOURCES
    needs_revision: bool = False
    terminated: bool = False
    termination_reason: str | None = None
    notes: List[str] = field(default_factory=list)
    acceptance_summary: Dict[str, bool] = field(default_factory=dict)
    evidence_pack: EvidencePack | None = None
    response: AgentResponse | None = None
    audit_report: AuditReport | None = None

    def unsatisfied_requirements(self) -> List[str]:
        return [
            requirement_id
            for requirement_id, evaluation in self.requirement_status.items()
            if not evaluation.satisfied
        ]


class ToolSuite:
    """Container bundling the available tool adapters."""

    def __init__(
        self,
        *,
        db: DatabaseTool | None = None,
        graph: GraphTool | None = None,
        web: WebTool | None = None,
        sequential: SequentialThinkingTool | None = None,
        neo4j_cypher: MCPJsonTool | None = None,
        neo4j_memory: MCPJsonTool | None = None,
        neo4j_modeling: MCPJsonTool | None = None,
        legal: MCPJsonTool | None = None,
        custom_agents: Dict[str, CustomAgentRunner] | None = None,
    ) -> None:
        self.sequential = sequential or SequentialThinkingTool(
            server_name="sequentialthinking",
            default_tool="sequentialthinking",
            id_prefix="seq",
        )
        self.db = db or DatabaseTool()
        self.graph = graph or GraphTool()
        self.web = web or WebTool(sequential_tool=self.sequential)
        self.neo4j_cypher = neo4j_cypher or MCPJsonTool(
            server_name="neo4j-cypher",
            default_tool="read_neo4j_cypher",
            id_prefix="neo4j",
        )
        self.neo4j_memory = neo4j_memory or MCPJsonTool(
            server_name="neo4j-memory",
            default_tool="search_memories",
            id_prefix="memory",
        )
        self.neo4j_modeling = neo4j_modeling or MCPJsonTool(
            server_name="neo4j-modeling",
            default_tool="list_example_data_models",
            id_prefix="model",
        )
        self.legal = legal or MCPJsonTool(
            server_name="legal",
            default_tool="search",
            id_prefix="legal",
        )
        self.agents = custom_agents or {}
        self.registry = self._build_registry()

    def _wrap(self, callable_obj):
        def _runner(task: PlanTask, requirement: Requirement) -> ToolOutcome:
            result = callable_obj(task, requirement)
            if isinstance(result, ToolOutcome):
                return result
            if isinstance(result, list):
                return ToolOutcome(evidence=list(result))
            return ToolOutcome()

        return _runner

    def _build_registry(self) -> Dict[str, Any]:
        registry = {
            "db_search": lambda task, requirement: self.db.execute(
                task,
                requirement,
                limit=policies.MAX_EVIDENCE_ITEMS,
            ),
            "graph_search": lambda task, requirement: self.graph.execute(task, requirement),
            "web_search": lambda task, requirement: self.web.execute(
                task,
                requirement,
                limit=policies.MAX_WEB_RESULTS,
            ),
            "agent-sequentialthinking": self._wrap(self.sequential.execute),
            "neo4j_cypher": self._wrap(self.neo4j_cypher.execute),
            "neo4j_memory": self._wrap(self.neo4j_memory.execute),
            "neo4j_modeling": self._wrap(self.neo4j_modeling.execute),
            "legal_search": self._wrap(self.legal.execute),
        }
        for name, runner in self.agents.items():
            registry[name] = self._wrap(runner.execute)
        return registry


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


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def _source_key(item: EvidenceItem) -> str:
    metadata = item.metadata or {}
    return metadata.get("source_uri") or metadata.get("uri") or item.source or item.id


def _authority_score(item: EvidenceItem) -> float:
    uri = (_source_key(item) or "").lower()
    if uri.startswith("https://"):
        score = 0.7
    else:
        score = 0.5
    if any(keyword in uri for keyword in ("court", "courts", ".gov", ".mil", "official")):
        score = 1.0
    elif any(keyword in uri for keyword in ("ballotpedia", "law.com", ".edu")):
        score = max(score, 0.85)
    return score


def _min_sources_for_requirement(requirement: Requirement, base: int, strict: int) -> int:
    quality = (requirement.quality_bar or "").lower()
    if any(keyword in quality for keyword in ("two", "independent", "corrobor", "multiple")):
        return max(base, strict)
    return max(1, base)


def _threshold_for_requirement(requirement: Requirement, default: float) -> float:
    quality = (requirement.quality_bar or "").lower()
    if any(keyword in quality for keyword in ("rigorous", "high", "official", "two")):
        return min(0.9, default + 0.1)
    return default


def _prepare_task_status(plan: Plan, existing: Dict[str, TaskStatus] | None = None) -> Dict[str, TaskStatus]:
    status = dict(existing or {})
    for task in plan.tasks:
        status.setdefault(task.id, TaskStatus.PENDING)
    return status


# ---------------------------------------------------------------------------
# Agent Framework executors
# ---------------------------------------------------------------------------


class InitializeRun(Executor):
    """Seed initial workflow state and process plan approval."""

    def __init__(self, *, approver: Approver, settings) -> None:
        super().__init__(id="initialize")
        self._approver = approver
        self._settings = settings

    @handler
    async def start(self, payload: WorkflowInput, ctx: WorkflowContext) -> None:
        plan = payload.plan
        run_logger = payload.run_logger
        report = ExecutionReport(run_id=payload.run_id)
        state = PlanLoopState(
            user_message=payload.user_message,
            plan=plan,
            report=report,
            run_logger=run_logger,
            deliver_response=payload.deliver_response,
            run_id=payload.run_id,
            max_iterations=self._settings.af_max_iterations,
            acceptance_threshold=self._settings.acceptance_confidence_threshold,
            base_min_sources=ACCEPTANCE_BASE_MIN_SOURCES,
            strict_min_sources=max(
                ACCEPTANCE_STRICT_MIN_SOURCES, self._settings.acceptance_min_sources
            ),
        )
        for requirement in plan.requirements:
            state.requirement_status[requirement.id] = RequirementEvaluation(
                requirement_id=requirement.id,
                min_sources=_min_sources_for_requirement(
                    requirement, state.base_min_sources, state.strict_min_sources
                ),
            )
        state.task_status = _prepare_task_status(plan)

        decision = self._approver.approve_plan(plan)
        if run_logger is not None:
            run_logger.log_event(
                "plan_approval",
                {"approved": decision.approved, "reason": decision.reason},
                status="approved" if decision.approved else "rejected",
            )
        if not decision.approved:
            state.terminated = True
            state.termination_reason = decision.reason or "Plan rejected"
            state.report.record(
                ExecutionEvent(
                    task_id="plan",
                    status=TaskStatus.FAILED,
                    message=state.termination_reason,
                )
            )
        await ctx.send_message(state)


class SelectNextAction(Executor):
    """Routing node that determines the next workflow step."""

    def __init__(
        self,
        *,
        execute_id: str,
        revise_id: str,
        respond_id: str,
        finalize_id: str,
    ) -> None:
        super().__init__(id="select_next_action")
        self._execute_id = execute_id
        self._revise_id = revise_id
        self._respond_id = respond_id
        self._finalize_id = finalize_id

    @handler
    async def route(self, state: PlanLoopState, ctx: WorkflowContext) -> None:
        if state.terminated:
            await ctx.send_message(state, target_id=self._finalize_id)
            return
        unsatisfied = state.unsatisfied_requirements()
        if not unsatisfied:
            if state.deliver_response:
                await ctx.send_message(state, target_id=self._respond_id)
            else:
                await ctx.send_message(state, target_id=self._finalize_id)
            return
        if state.iteration >= state.max_iterations:
            state.terminated = True
            state.termination_reason = (
                "Acceptance criteria not met after iteration limit"
            )
            state.report.record(
                ExecutionEvent(
                    task_id="loop",
                    status=TaskStatus.FAILED,
                    message=state.termination_reason,
                )
            )
            await ctx.send_message(state, target_id=self._finalize_id)
            return
        if state.needs_revision:
            await ctx.send_message(state, target_id=self._revise_id)
            return
        await ctx.send_message(state, target_id=self._execute_id)


class ExecuteTasks(Executor):
    """Execute ready tasks against the tool suite."""

    def __init__(
        self,
        *,
        tools: ToolSuite,
        approver: Approver,
        selector_id: str,
    ) -> None:
        super().__init__(id="execute_tasks")
        self._tools = tools
        self._approver = approver
        self._selector_id = selector_id

    @handler
    async def run_tasks(self, state: PlanLoopState, ctx: WorkflowContext) -> None:
        if state.terminated:
            await ctx.send_message(state, target_id=self._selector_id)
            return
        target_requirements = set(state.unsatisfied_requirements())
        ready_tasks = [
            task
            for task in sorted(state.plan.tasks, key=lambda t: (t.priority, t.id))
            if state.task_status.get(task.id, TaskStatus.PENDING) == TaskStatus.PENDING
            and task.requirement_id in target_requirements
            and all(
                state.task_status.get(dep) == TaskStatus.COMPLETED for dep in task.depends_on
            )
        ]
        if not ready_tasks:
            state.needs_revision = True
            state.notes.append("No ready tasks for unsatisfied requirements; requesting revision")
            await ctx.send_message(state, target_id=self._selector_id)
            return

        for task in ready_tasks:
            requirement = state.plan.requirement(task.requirement_id)
            policy_decision = policies.enforce_task_limits(task)
            if not policy_decision.allowed:
                state.task_status[task.id] = TaskStatus.FAILED
                message = f"Policy rejected task: {policy_decision.reason}"
                state.report.record(
                    ExecutionEvent(task_id=task.id, status=TaskStatus.FAILED, message=message)
                )
                if state.run_logger is not None:
                    state.run_logger.log_tool_result(
                        task_id=task.id,
                        tool=task.tool,
                        status="policy_rejected",
                        inputs=_safe_task_inputs(task.inputs),
                        error=policy_decision.reason,
                    )
                continue
            if task.requires_approval:
                approval = self._approver.approve_task(task)
                if state.run_logger is not None:
                    state.run_logger.log_event(
                        "task_approval",
                        {"approved": approval.approved, "reason": approval.reason},
                        task_id=task.id,
                        tool=task.tool,
                        status="approved" if approval.approved else "rejected",
                    )
                if not approval.approved:
                    state.task_status[task.id] = TaskStatus.FAILED
                    state.report.record(
                        ExecutionEvent(
                            task_id=task.id,
                            status=TaskStatus.FAILED,
                            message=approval.reason or "Task rejected",
                        )
                    )
                    continue

            state.task_status[task.id] = TaskStatus.RUNNING
            state.report.record(
                ExecutionEvent(
                    task_id=task.id,
                    status=TaskStatus.RUNNING,
                    message=f"tool={task.tool}",
                )
            )
            if state.run_logger is not None:
                state.run_logger.log_event(
                    "task_started",
                    {"inputs": _safe_task_inputs(task.inputs)},
                    task_id=task.id,
                    tool=task.tool,
                    status="running",
                )
            outcome = await asyncio.to_thread(self._run_task, state, task, requirement)
            if outcome is None:
                continue
            state.evidence.extend(outcome.evidence)
            state.findings.extend(outcome.findings)
            state.task_status[task.id] = TaskStatus.COMPLETED
            state.report.record(
                ExecutionEvent(
                    task_id=task.id,
                    status=TaskStatus.COMPLETED,
                    message=f"items={len(outcome.evidence)} findings={len(outcome.findings)}",
                )
            )
            if outcome.notes:
                state.notes.extend(outcome.notes)
            if state.run_logger is not None:
                state.run_logger.log_tool_result(
                    task_id=task.id,
                    tool=task.tool,
                    status="completed",
                    inputs=_safe_task_inputs(task.inputs),
                    outputs=outcome.evidence,
                    findings=outcome.findings,
                )
        await ctx.send_message(state)

    def _run_task(
        self, state: PlanLoopState, task: PlanTask, requirement: Requirement
    ) -> ToolOutcome | None:
        runner = self._tools.registry.get(task.tool)
        if runner is None:
            state.task_status[task.id] = TaskStatus.FAILED
            message = f"Unknown tool: {task.tool}"
            state.report.record(
                ExecutionEvent(task_id=task.id, status=TaskStatus.FAILED, message=message)
            )
            if state.run_logger is not None:
                state.run_logger.log_tool_result(
                    task_id=task.id,
                    tool=task.tool,
                    status="failed",
                    inputs=_safe_task_inputs(task.inputs),
                    error=message,
                )
            return None
        enriched_inputs = dict(task.inputs)
        enriched_inputs.setdefault("_run_id", state.run_id)
        enriched_inputs.setdefault("_requirement_id", requirement.id)
        enriched_inputs.setdefault("_task_id", task.id)
        enriched_inputs.setdefault("_iteration", state.iteration)
        task_for_tool = task.model_copy(update={"inputs": enriched_inputs})
        try:
            return runner(task_for_tool, requirement)
        except Exception as exc:  # pragma: no cover - defensive
            state.task_status[task.id] = TaskStatus.FAILED
            message = f"Tool execution failed: {exc}"[:400]
            LOGGER.exception("Tool execution failed", extra={"task": task.id, "tool": task.tool})
            state.report.record(
                ExecutionEvent(task_id=task.id, status=TaskStatus.FAILED, message=message)
            )
            if state.run_logger is not None:
                state.run_logger.log_tool_result(
                    task_id=task.id,
                    tool=task.tool,
                    status="failed",
                    inputs=_safe_task_inputs(task.inputs),
                    error=str(exc),
                )
            return ToolOutcome(notes=[message])


class CurateEvidence(Executor):
    """Deduplicate evidence, enforce budgets, and run evidence approval gate."""

    def __init__(self, *, approver: Approver, selector_id: str) -> None:
        super().__init__(id="curate_evidence")
        self._approver = approver
        self._selector_id = selector_id

    @handler
    async def curate(self, state: PlanLoopState, ctx: WorkflowContext) -> None:
        if state.terminated:
            await ctx.send_message(state, target_id=self._selector_id)
            return
        if not state.evidence:
            state.notes.append("No evidence collected during iteration")
            await ctx.send_message(state, target_id=self._selector_id)
            return
        state.evidence_pack = assemble_evidence(state.evidence)
        if state.run_logger is not None:
            state.run_logger.log_evidence(state.evidence_pack)
        decision = self._approver.approve_evidence(state.evidence_pack)
        if state.run_logger is not None:
            state.run_logger.log_event(
                "evidence_approval",
                {"approved": decision.approved, "reason": decision.reason},
                status="approved" if decision.approved else "rejected",
            )
        if not decision.approved:
            state.terminated = True
            state.termination_reason = decision.reason or "Evidence pack rejected"
            state.report.record(
                ExecutionEvent(
                    task_id="evidence_gate",
                    status=TaskStatus.FAILED,
                    message=state.termination_reason,
                )
            )
        await ctx.send_message(state, target_id=self._selector_id)


class EvaluateGaps(Executor):
    """Compute acceptance metrics and determine whether revision is required."""

    def __init__(self, *, selector_id: str) -> None:
        super().__init__(id="evaluate_gaps")
        self._selector_id = selector_id

    @handler
    async def evaluate(self, state: PlanLoopState, ctx: WorkflowContext) -> None:
        if state.terminated:
            await ctx.send_message(state, target_id=self._selector_id)
            return
        findings_by_requirement: Dict[str, List[Finding]] = defaultdict(list)
        for finding in state.findings:
            findings_by_requirement[finding.requirement_id].append(finding)
        evidence_by_id = {item.id: item for item in state.evidence}
        state.open_questions = []
        all_satisfied = True
        acceptance_summary: Dict[str, bool] = {}

        for requirement in state.plan.requirements:
            evaluation = state.requirement_status.setdefault(
                requirement.id,
                RequirementEvaluation(
                    requirement_id=requirement.id,
                    min_sources=_min_sources_for_requirement(
                        requirement, state.base_min_sources, state.strict_min_sources
                    ),
                ),
            )
            evaluation.issues = []
            supporting_findings = findings_by_requirement.get(requirement.id, [])
            supporting_ids = sorted(
                {evidence_id for finding in supporting_findings for evidence_id in finding.evidence_ids}
            )
            supporting_items = [
                evidence_by_id[evidence_id]
                for evidence_id in supporting_ids
                if evidence_id in evidence_by_id
            ]
            unique_sources = { _source_key(item) for item in supporting_items }
            evaluation.supporting_evidence_ids = supporting_ids
            if supporting_items:
                authority_scores = [_authority_score(item) for item in supporting_items]
                evaluation.authority_score = sum(authority_scores) / len(authority_scores)
                finding_conf = [finding.confidence for finding in supporting_findings]
                evaluation.quality_score = sum(finding_conf) / len(finding_conf)
                evaluation.agreement_score = min(
                    1.0, len(unique_sources) / max(1, evaluation.min_sources)
                )
                evaluation.confidence = min(
                    1.0,
                    0.45 * evaluation.agreement_score
                    + 0.35 * evaluation.quality_score
                    + 0.20 * evaluation.authority_score,
                )
            else:
                evaluation.authority_score = 0.0
                evaluation.quality_score = 0.0
                evaluation.agreement_score = 0.0
                evaluation.confidence = 0.0

            required_sources = evaluation.min_sources
            if len(unique_sources) < required_sources:
                evaluation.issues.append(
                    f"Needs {required_sources} unique sources, found {len(unique_sources)}"
                )
            threshold = _threshold_for_requirement(requirement, state.acceptance_threshold)
            evaluation.satisfied = (
                evaluation.confidence >= threshold and len(unique_sources) >= required_sources
            )
            if not evaluation.satisfied:
                all_satisfied = False
                reason = ", ".join(evaluation.issues) if evaluation.issues else "Confidence below threshold"
                state.open_questions.append(
                    OpenQuestion(
                        question=requirement.question,
                        reason=reason,
                        requirement_id=requirement.id,
                    )
                )
            acceptance_summary[requirement.question] = evaluation.satisfied

        state.iteration += 1
        state.needs_revision = not all_satisfied and not state.terminated
        state.acceptance_summary = acceptance_summary
        state.plan.open_questions = list(state.open_questions)
        await ctx.send_message(state, target_id=self._selector_id)


class RevisePlan(Executor):
    """Invoke the planner to revise tasks based on new evidence."""

    def __init__(self, *, selector_id: str) -> None:
        super().__init__(id="revise_plan")
        self._selector_id = selector_id

    @handler
    async def revise(self, state: PlanLoopState, ctx: WorkflowContext) -> None:
        if state.terminated:
            await ctx.send_message(state, target_id=self._selector_id)
            return
        try:
            from ..planner.main_planner import revise_plan_with_evidence  # local import to avoid cycles

            revised_plan = await asyncio.to_thread(
                revise_plan_with_evidence,
                state.plan,
                user_message=state.user_message,
                new_findings=tuple(state.findings),
                new_open_questions=tuple(state.open_questions),
                logger=state.run_logger,
            )
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.exception("Plan revision failed")
            state.terminated = True
            state.termination_reason = f"Plan revision failed: {exc}"[:400]
            state.report.record(
                ExecutionEvent(
                    task_id="plan_revision",
                    status=TaskStatus.FAILED,
                    message=state.termination_reason,
                )
            )
            await ctx.send_message(state, target_id=self._selector_id)
            return

        state.plan = revised_plan
        state.requirement_status = {
            requirement.id: state.requirement_status.get(
                requirement.id,
                RequirementEvaluation(
                    requirement_id=requirement.id,
                    min_sources=_min_sources_for_requirement(
                        requirement, state.base_min_sources, state.strict_min_sources
                    ),
                ),
            )
            for requirement in revised_plan.requirements
        }
        state.task_status = _prepare_task_status(revised_plan, state.task_status)
        state.needs_revision = False
        await ctx.send_message(state, target_id=self._selector_id)


class Respond(Executor):
    """Generate the final response, run approvals, and trigger the audit agent."""

    def __init__(
        self,
        *,
        responder: Responder,
        approver: Approver,
        audit_agent: AuditAgent | None,
        finalize_id: str,
    ) -> None:
        super().__init__(id="respond")
        self._responder = responder
        self._approver = approver
        self._audit_agent = audit_agent
        self._finalize_id = finalize_id

    @handler
    async def respond(self, state: PlanLoopState, ctx: WorkflowContext) -> None:
        if state.terminated:
            await ctx.send_message(state, target_id=self._finalize_id)
            return
        evidence_pack = state.evidence_pack or assemble_evidence(state.evidence)
        response = await asyncio.to_thread(
            self._responder.respond,
            state.user_message,
            evidence_pack,
            findings=tuple(state.findings),
            acceptance_criteria=tuple(state.plan.acceptance_criteria),
            open_questions=tuple(state.open_questions),
        )
        approval = self._approver.approve_response(response)
        if state.run_logger is not None:
            state.run_logger.log_response(response)
            state.run_logger.log_event(
                "response_approval",
                {"approved": approval.approved, "reason": approval.reason},
                status="approved" if approval.approved else "rejected",
            )
        if not approval.approved:
            state.report.record(
                ExecutionEvent(
                    task_id="final_response",
                    status=TaskStatus.FAILED,
                    message=approval.reason or "Response rejected",
                )
            )
            state.response = AgentResponse(
                answer="Final response requires revision before delivery.",
                citations=[],
                confidence=0.0,
                unresolved_questions=[approval.reason or "Manual review required."],
            )
            state.terminated = True
            state.termination_reason = approval.reason or "Response rejected"
            await ctx.send_message(state, target_id=self._finalize_id)
            return

        audit_report: AuditReport | None = None
        if self._audit_agent is not None:
            audit_report = await asyncio.to_thread(
                self._audit_agent.evaluate,
                question=state.user_message,
                plan=state.plan,
                evidence=evidence_pack,
                response=response,
            )
            if state.run_logger is not None:
                state.run_logger.log_audit(audit_report)
            if not audit_report.passed:
                state.report.record(
                    ExecutionEvent(
                        task_id="audit_gate",
                        status=TaskStatus.FAILED,
                        message=audit_report.summary or "Audit failed",
                    )
                )
                state.terminated = True
                state.termination_reason = audit_report.summary or "Audit failed"

        state.response = response
        state.evidence_pack = evidence_pack
        state.audit_report = audit_report
        await ctx.send_message(state, target_id=self._finalize_id)


class Finalize(Executor):
    """Terminal node that emits the final state."""

    def __init__(self) -> None:
        super().__init__(id="finalize")

    @handler
    async def finalize(self, state: PlanLoopState, ctx: WorkflowContext) -> None:
        await ctx.yield_output(state)


# ---------------------------------------------------------------------------
# Public orchestrator API
# ---------------------------------------------------------------------------


class WorkflowOrchestrator:
    """Execute plans using the Microsoft Agent Framework workflow engine."""

    def __init__(
        self,
        db_tool: DatabaseTool | None = None,
        graph_tool: GraphTool | None = None,
        web_tool: WebTool | None = None,
        responder: Responder | None = None,
        approver: Approver | None = None,
        custom_agents: Dict[str, CustomAgentRunner] | None = None,
        audit_agent: AuditAgent | None = None,
    ) -> None:
        settings = get_settings()
        self.tools = ToolSuite(
            db=db_tool,
            graph=graph_tool,
            web=web_tool,
            custom_agents=custom_agents or self._build_custom_agents(),
        )
        self.responder = responder or Responder(model_name=settings.responder_model)
        self.approver = approver or AutoApprover()
        if audit_agent is None:
            try:
                audit_agent = AuditAgent()
            except FileNotFoundError:
                LOGGER.warning("Audit agent configuration missing; disabling audit gate")
                audit_agent = None
        self.audit_agent = audit_agent
        self.settings = settings

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

    def execute_plan(
        self,
        plan: Plan,
        *,
        message: str | None = None,
        run_logger: AgentRunLogger | None = None,
    ) -> ExecutionResult:
        token = push_run_logger(run_logger) if run_logger is not None else None
        try:
            state = self._run_workflow(
                WorkflowInput(
                    user_message=message or plan.problem_spec,
                    plan=plan,
                    deliver_response=False,
                    run_logger=run_logger,
                    run_id=str(run_logger.run_id) if run_logger else generate_run_id(),
                )
            )
        finally:
            if token is not None:
                reset_run_logger(token)
        evidence_pack = state.evidence_pack or assemble_evidence(state.evidence)
        return ExecutionResult(
            evidence=evidence_pack,
            report=state.report,
            audit_report=state.audit_report,
        )

    def run_pipeline(
        self,
        message: str,
        plan: Plan,
        *,
        run_logger: AgentRunLogger | None = None,
    ) -> tuple[AgentResponse, ExecutionResult]:
        token = push_run_logger(run_logger) if run_logger is not None else None
        try:
            state = self._run_workflow(
                WorkflowInput(
                    user_message=message,
                    plan=plan,
                    deliver_response=True,
                    run_logger=run_logger,
                    run_id=str(run_logger.run_id) if run_logger else generate_run_id(),
                )
            )
        finally:
            if token is not None:
                reset_run_logger(token)
        evidence_pack = state.evidence_pack or assemble_evidence(state.evidence)
        if state.response is None:
            response = AgentResponse(
                answer="Final response requires review.",
                citations=evidence_pack.citation_order(),
                confidence=0.0,
                unresolved_questions=[state.termination_reason or "Workflow ended early."],
            )
        else:
            response = state.response
        result = ExecutionResult(
            evidence=evidence_pack,
            report=state.report,
            audit_report=state.audit_report,
        )
        return response, result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_workflow(self) -> Any:
        selector = SelectNextAction(
            execute_id="execute_tasks",
            revise_id="revise_plan",
            respond_id="respond",
            finalize_id="finalize",
        )
        execute = ExecuteTasks(
            tools=self.tools,
            approver=self.approver,
            selector_id=selector.id,
        )
        curate = CurateEvidence(approver=self.approver, selector_id=selector.id)
        evaluate = EvaluateGaps(selector_id=selector.id)
        revise = RevisePlan(selector_id=selector.id)
        respond = Respond(
            responder=self.responder,
            approver=self.approver,
            audit_agent=self.audit_agent,
            finalize_id="finalize",
        )
        finalize = Finalize()
        initialize = InitializeRun(approver=self.approver, settings=self.settings)

        builder = (
            WorkflowBuilder()
            .add_edge(initialize, selector)
            .add_edge(selector, execute)
            .add_edge(selector, revise)
            .add_edge(selector, respond)
            .add_edge(selector, finalize)
            .add_edge(execute, curate)
            .add_edge(execute, selector)
            .add_edge(curate, evaluate)
            .add_edge(curate, selector)
            .add_edge(evaluate, selector)
            .add_edge(revise, selector)
            .add_edge(respond, finalize)
            .set_start_executor(initialize)
        )
        return builder.build()

    def _run_workflow(self, payload: WorkflowInput) -> PlanLoopState:
        workflow = self._build_workflow()
        events = asyncio.run(workflow.run(payload))
        outputs = events.get_outputs()
        if not outputs:
            LOGGER.error("Workflow produced no outputs; returning empty state")
            empty_report = ExecutionReport(run_id=payload.run_id, successful=False)
            return PlanLoopState(
                user_message=payload.user_message,
                plan=payload.plan,
                report=empty_report,
                run_logger=payload.run_logger,
                deliver_response=payload.deliver_response,
                run_id=payload.run_id,
                terminated=True,
                termination_reason="Workflow produced no outputs",
            )
        final_state = outputs[-1]
        assert isinstance(final_state, PlanLoopState)
        if final_state.terminated and final_state.report.successful:
            final_state.report.successful = False
        return final_state


def run_tool(tools: ToolSuite, task: PlanTask, requirement: Requirement) -> ToolOutcome:
    """Execute a single plan task using the provided tool suite."""

    runner = tools.registry.get(task.tool)
    if runner is None:
        raise KeyError(f"Unknown tool: {task.tool}")
    return runner(task, requirement)


def _safe_task_inputs(raw: object) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    safe: Dict[str, Any] = {}
    for key, value in raw.items():
        try:
            safe[str(key)] = value if isinstance(value, (int, float, str, bool)) else str(value)
        except Exception:  # pragma: no cover - defensive
            safe[str(key)] = repr(value)
    return safe


__all__ = ["ToolSuite", "WorkflowOrchestrator", "assemble_evidence", "run_tool"]
