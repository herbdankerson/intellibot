"""Human-in-the-loop approval helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..schemas import EvidencePack, Plan, PlanTask

if TYPE_CHECKING:
    from ..schemas import AgentResponse


@dataclass
class ApprovalResult:
    """Represents the outcome of an approval checkpoint."""

    approved: bool
    reason: str = ""


class Approver:
    """Abstract approver interface."""

    def approve_plan(self, plan: Plan) -> ApprovalResult:  # pragma: no cover - simple stub
        return ApprovalResult(approved=True)

    def approve_task(self, task: PlanTask) -> ApprovalResult:  # pragma: no cover
        return ApprovalResult(approved=True)

    def approve_response(self, response: "AgentResponse") -> ApprovalResult:  # pragma: no cover
        return ApprovalResult(approved=True)

    def approve_evidence(self, pack: EvidencePack) -> ApprovalResult:  # pragma: no cover
        return ApprovalResult(approved=True)


class AutoApprover(Approver):
    """Approver that automatically approves every checkpoint."""

    pass


__all__ = ["ApprovalResult", "Approver", "AutoApprover"]
