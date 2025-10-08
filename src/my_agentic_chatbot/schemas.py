"""Pydantic schemas shared across the service."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Set

from pydantic import BaseModel, Field, model_validator


class TaskStatus(str, Enum):
    """Lifecycle states for workflow tasks."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class Requirement(BaseModel):
    """A unit of information the planner aims to satisfy."""

    id: str = Field(..., description="Stable identifier for the requirement (e.g. req-1).")
    question: str = Field(..., description="Natural language question to be answered.")
    priority: int = Field(
        default=1,
        ge=1,
        description="Lower numbers indicate higher priority when executing tasks.",
    )
    quality_bar: str = Field(
        default="At least one high-quality, citable source.",
        description="Expectation for evidence quality before the requirement is considered satisfied.",
    )
    stop_when_satisfied: bool = Field(
        default=True,
        description="If true, the planner may omit further tasks once confidence is high enough.",
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Optional planner metadata (suggested entities, keywords, etc.).",
    )


class PlanTask(BaseModel):
    """A single actionable step emitted by the planner."""

    id: str = Field(..., description="Stable identifier for the task.")
    requirement_id: str = Field(
        ..., description="Identifier of the requirement this task primarily serves."
    )
    description: str = Field(
        ..., description="Natural language description of the task and desired outcome."
    )
    tool: str = Field(..., description="Name of the tool agent expected to execute the task.")
    priority: int = Field(
        default=1,
        ge=1,
        description="Lower numbers indicate earlier execution preference for the workflow designer.",
    )
    budget_tokens: int = Field(
        ..., gt=0, description="Maximum tokens the task may consume, including sub-delegations."
    )
    timeout_seconds: int = Field(..., gt=0, description="Execution timeout budget.")
    requires_approval: bool = Field(
        default=False, description="Whether human approval is required before execution."
    )
    inputs: Dict[str, Any] = Field(
        default_factory=dict,
        description="Structured inputs for the downstream tool or agent.",
    )
    depends_on: List[str] = Field(
        default_factory=list,
        description="Identifiers of prior tasks that must complete successfully first.",
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Optional planner annotations (e.g. suggested follow-up heuristics).",
    )


class Finding(BaseModel):
    """Structured claim derived from collected evidence."""

    id: str = Field(..., description="Stable identifier for referencing within the workflow.")
    requirement_id: Optional[str] = Field(
        default=None, description="Requirement this finding supports, if any."
    )
    key: str = Field(..., description="Short label describing the claim.")
    value: str = Field(..., description="The textual content of the claim.")
    confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Planner-estimated confidence that the claim is correct.",
    )
    evidence_ids: List[str] = Field(
        default_factory=list,
        description="Identifiers of evidence items supporting the claim.",
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Optional annotations (e.g. reasoning notes, disagreement flags).",
    )


class OpenQuestion(BaseModel):
    """Outstanding question the planner wants to resolve in later loops."""

    question: str = Field(..., description="Natural language description of the gap.")
    reason: str = Field(
        default="",
        description="Why this question matters (acceptance criteria, contradictions, etc.).",
    )
    requirement_id: Optional[str] = Field(
        default=None,
        description="Requirement associated with this open question, if known.",
    )
    suggested_tasks: List[str] = Field(
        default_factory=list,
        description="Planner hints for future tasks that may close the gap.",
    )


class Plan(BaseModel):
    """Planner output that the orchestrator consumes."""

    problem_spec: str = Field(
        ..., description="Concise restatement of the user's request and key context."
    )
    acceptance_criteria: List[str] = Field(default_factory=list)
    requirements: List[Requirement] = Field(default_factory=list)
    tasks: List[PlanTask] = Field(default_factory=list)
    findings: List[Finding] = Field(default_factory=list)
    open_questions: List[OpenQuestion] = Field(default_factory=list)
    stop_conditions: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Free-form planner metadata for the orchestrator."
    )

    @model_validator(mode="after")
    def _validate_relationships(self) -> "Plan":
        requirement_ids: Set[str] = {req.id for req in self.requirements}
        if len(requirement_ids) != len(self.requirements):
            raise ValueError("Requirements must have unique identifiers")

        for task in self.tasks:
            if task.requirement_id not in requirement_ids:
                raise ValueError(
                    f"Task {task.id} references unknown requirement {task.requirement_id}"
                )
            if task.budget_tokens <= 0:
                raise ValueError(f"Task {task.id} must declare a positive token budget")
            if task.timeout_seconds <= 0:
                raise ValueError(f"Task {task.id} must declare a positive timeout")

        valid_task_ids: Set[str] = {task.id for task in self.tasks}
        for task in self.tasks:
            dangling = [dep for dep in task.depends_on if dep not in valid_task_ids]
            if dangling:
                raise ValueError(
                    f"Task {task.id} declares unknown dependencies: {', '.join(dangling)}"
                )

        return self

    def requirement(self, requirement_id: str) -> Requirement:
        """Return the requirement with the given identifier."""

        for requirement in self.requirements:
            if requirement.id == requirement_id:
                return requirement
        raise KeyError(requirement_id)

    def as_dict(self) -> Dict[str, Any]:
        """Dump the plan as a JSON-serializable dictionary."""

        return self.model_dump()


class AuditFinding(BaseModel):
    """Issue or confirmation emitted by the audit agent."""

    severity: Literal["info", "warning", "error"]
    message: str
    citations: List[str] = Field(default_factory=list)
    acceptance_criteria: List[str] = Field(default_factory=list)


class AuditReport(BaseModel):
    """Structured payload produced by the audit agent."""

    passed: bool
    findings: List[AuditFinding] = Field(default_factory=list)
    coverage: Dict[str, bool] = Field(default_factory=dict)
    summary: str = Field(default="")


class EvidenceItem(BaseModel):
    """A single piece of evidence collected from a tool agent."""

    id: str
    source: str
    content: str
    score: float = Field(default=1.0, ge=0.0)
    metadata: Dict[str, str] = Field(default_factory=dict)

    def token_estimate(self) -> int:
        """Rough heuristic to approximate token count of the content."""

        return max(1, len(self.content.split()))


class EvidencePack(BaseModel):
    """Compact collection of evidence items used by the responder."""

    items: List[EvidenceItem] = Field(default_factory=list)
    summary: str = Field(default="", description="High-level summary of collected evidence.")
    token_count: int = Field(default=0, ge=0)
    truncated: bool = Field(default=False)

    @model_validator(mode="after")
    def _populate_token_count(self) -> "EvidencePack":
        if not self.token_count:
            self.token_count = sum(item.token_estimate() for item in self.items)
        return self

    def citation_order(self) -> List[str]:
        """Return the order of evidence identifiers for the responder."""

        return [item.id for item in self.items]


class ExecutionEvent(BaseModel):
    """Single event recorded during workflow execution."""

    task_id: str
    status: TaskStatus
    message: str = Field(default="")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ExecutionReport(BaseModel):
    """Aggregate record describing the run."""

    run_id: str
    events: List[ExecutionEvent] = Field(default_factory=list)
    successful: bool = True
    notes: List[str] = Field(default_factory=list)

    def record(self, event: ExecutionEvent) -> None:
        """Append an execution event to the report."""

        self.events.append(event)
        if event.status == TaskStatus.FAILED:
            self.successful = False


class AgentResponse(BaseModel):
    """Structured response returned to the client."""

    answer: str
    citations: List[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    unresolved_questions: List[str] = Field(default_factory=list)


class ExecutionResult(BaseModel):
    """Convenience wrapper combining evidence, reports, and audits."""

    evidence: EvidencePack
    report: ExecutionReport
    audit_report: AuditReport | None = None


class UserQuery(BaseModel):
    """Incoming request payload for the `/run` endpoint."""

    message: str = Field(..., min_length=1, max_length=4000)


class UrlIngestRequest(BaseModel):
    """Payload for enqueuing URL-based ingests."""

    urls: List[str] = Field(..., min_length=1)
    labels: Optional[List[str]] = Field(default=None)
    crawl_depth: int = Field(default=0, ge=0, le=3)


class IngestJobStatus(BaseModel):
    """Summary returned to clients after initiating an ingest."""

    ingest_item_id: str
    job_id: str
    status: str
    source_type: str
    display_name: str
    domain: Optional[str] = None
    domain_confidence: Optional[float] = None
    chunk_count: int = 0
    embedding_spaces: List[str] = Field(default_factory=list)
    metadata: Dict[str, object] = Field(default_factory=dict)
    document_summary: Optional[str] = None


__all__ = [
    "AgentResponse",
    "AuditFinding",
    "AuditReport",
    "Finding",
    "EvidenceItem",
    "EvidencePack",
    "ExecutionEvent",
    "ExecutionReport",
    "ExecutionResult",
    "OpenQuestion",
    "Plan",
    "PlanTask",
    "Requirement",
    "TaskStatus",
    "IngestJobStatus",
    "UrlIngestRequest",
    "UserQuery",
]
