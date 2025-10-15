"""Simplified tool outcome types for the stubbed tool suite."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from ..schemas import EvidenceItem, Finding


@dataclass
class ToolOutcome:
    """Container returned by stub tools to mimic real adapters."""

    evidence: List[EvidenceItem] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


__all__ = ["ToolOutcome"]
