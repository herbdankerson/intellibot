"""Pydantic schemas shared across the service."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class TaskStatus(str, Enum):
    """Lifecycle states for workflow tasks."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanTask(BaseModel):
    """A single actionable step emitted by the planner."""

    id: str = Field(..., description="Stable identifier for the task.")
    description: str = Field(..., description="Natural language description of the task.")
    tool: str = Field(..., description="Name of the tool agent expected to execute the task.")
    budget_tokens: int = Field(..., gt=0, description="Maximum tokens the task may consume.")
    timeout_seconds: int = Field(..., gt=0, description="Execution timeout budget.")
    requires_approval: bool = Field(
        default=False, description="Whether human approval is required before execution."
    )
    inputs: Dict[str, Any] = Field(
        default_factory=dict,
        description="Structured inputs the workflow designer may bind to tool arguments.",
    )
    depends_on: List[str] = Field(
        default_factory=list,
        description="Identifiers of prior tasks that must complete successfully first.",
    )


class Plan(BaseModel):
    """Planner output that the orchestrator consumes."""

    goals: List[str] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)
    info_needed: List[str] = Field(default_factory=list)
    tasks: List[PlanTask] = Field(default_factory=list)
    stop_conditions: List[str] = Field(default_factory=list)
    acceptance_criteria: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ensure_task_budgets(cls, model: "Plan") -> "Plan":  # type: ignore[override]
        for task in model.tasks:
            if task.budget_tokens <= 0:
                raise ValueError("Tasks must declare a positive token budget.")
            if task.timeout_seconds <= 0:
                raise ValueError("Tasks must declare a positive timeout.")
            if not isinstance(task.depends_on, list):
                raise ValueError("Task depends_on must be a list of task identifiers.")
        return model


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
    def _populate_token_count(cls, model: "EvidencePack") -> "EvidencePack":  # type: ignore[override]
        if not model.token_count:
            model.token_count = sum(item.token_estimate() for item in model.items)
        return model

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
    "EvidenceItem",
    "EvidencePack",
    "ExecutionEvent",
    "ExecutionReport",
    "ExecutionResult",
    "Plan",
    "PlanTask",
    "TaskStatus",
    "IngestJobStatus",
    "UrlIngestRequest",
    "UserQuery",
]
