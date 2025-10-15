"""Stub sequential guidance module used during tool stubbing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class GuidanceResult:
    notes: List[str] = field(default_factory=list)
    queries: List[str] = field(default_factory=list)
    priority_urls: List[str] = field(default_factory=list)

    def as_note(self) -> str:
        return "stub sequential guidance active"


class SequentialGuidance:
    """Returns canned follow-up notes so the orchestrator can proceed."""

    def analyse(self, *_, **__) -> GuidanceResult:
        return GuidanceResult(
            notes=["stub guidance used"],
            queries=["stub follow-up"],
            priority_urls=["https://example.com/stub"],
        )


__all__ = ["GuidanceResult", "SequentialGuidance"]
