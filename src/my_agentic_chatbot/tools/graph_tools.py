"""Graph query helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from ..schemas import EvidenceItem
from ..util.text import build_snippet


@dataclass
class GraphTool:
    """Stub implementation that simulates Cypher queries."""

    snippet_chars: int = 280

    def search(self, query: str) -> List[EvidenceItem]:
        """Return deterministic evidence for the provided query."""

        content = (
            "Graph exploration indicates multi-hop reasoning should be guarded. "
            "This stub simply echoes the query to demonstrate plumbing."
        )
        item = EvidenceItem(
            id="graph-1",
            source="neo4j",
            content=build_snippet(content, self.snippet_chars),
            score=0.5,
            metadata={"cypher": "// simulated"},
        )
        return [item]


__all__ = ["GraphTool"]
