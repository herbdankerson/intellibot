"""Stub implementation of the database tool for end-to-end smoke runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional

from ..schemas import EvidenceItem, Finding, PlanTask, Requirement
from .types import ToolOutcome

_STUB_EVIDENCE_TEXT = [
    "Project instructions summarised: ensure KB-first planning, sandbox promotion, and agent-scoped tools.",
    "Knowledge base entry: ingestion embeds every document and chat message with idempotent retries.",
    "Operational note: LiteLLM fronts Gemini 2.5 Flash for planning until larger models are provisioned.",
]


@dataclass
class DatabaseTool:
    """Returns canned evidence without touching external services."""

    max_results: int = 3

    def search(
        self,
        query: str,
        *,
        limit: Optional[int] = None,
        timeout_seconds: Optional[int] = None,
        scope: Optional[Iterable[str]] = None,
    ) -> List[EvidenceItem]:
        if not query.strip():
            return []
        capped = min(limit or self.max_results, self.max_results)
        return [
            EvidenceItem(
                id=f"stub-db-{index + 1}",
                source="stub-database",
                content=text,
                score=1.0 - index * 0.1,
                metadata={"scope": ",".join(scope or []) or "default", "stub": "true"},
            )
            for index, text in enumerate(_STUB_EVIDENCE_TEXT[:capped])
        ]

    def execute(
        self,
        task: PlanTask,
        requirement: Requirement,
        *,
        limit: Optional[int] = None,
        scope: Optional[Iterable[str]] = None,
        overrides: Optional[dict] = None,
    ) -> ToolOutcome:
        query = (
            str(task.inputs.get("query"))
            if isinstance(task.inputs, dict) and task.inputs.get("query")
            else task.description or requirement.question or ""
        )
        evidence = self.search(query, limit=limit, scope=scope)
        findings = [
            Finding(
                id=f"finding-db-{index + 1}",
                requirement_id=requirement.id,
                key=requirement.question or requirement.id,
                value=item.content,
                confidence=0.6,
                evidence_ids=[item.id],
                metadata={"source": item.source, "stub": "true"},
            )
            for index, item in enumerate(evidence)
        ]
        notes = [] if evidence else ["stub database returned no evidence"]
        return ToolOutcome(evidence=evidence, findings=findings, notes=notes)


__all__ = ["DatabaseTool"]
