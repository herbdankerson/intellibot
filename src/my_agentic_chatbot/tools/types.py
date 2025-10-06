"""Shared tool adapter types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from ..schemas import EvidenceItem, Finding


@dataclass
class ToolOutcome:
    """Normalized output from tool adapters for orchestrator consumption."""

    evidence: List[EvidenceItem] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


__all__ = ["ToolOutcome"]
