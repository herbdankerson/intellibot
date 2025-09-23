"""Web search helper used by the workflow orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from ..schemas import EvidenceItem
from ..util.text import build_snippet


@dataclass
class WebTool:
    """Stubbed web search implementation."""

    snippet_chars: int = 240

    def search(self, query: str) -> List[EvidenceItem]:
        """Return a placeholder web result for local development."""

        body = (
            "External web search is optional for the starter stack. In production "
            "this would call an MCP web server with rate limits."
        )
        item = EvidenceItem(
            id="web-1",
            source="web",
            content=build_snippet(body, self.snippet_chars),
            score=0.2,
            metadata={"provider": "stub"},
        )
        return [item]


__all__ = ["WebTool"]
