"""Responder that assembles the final answer from evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from ..llm_calls.llm_client import LLMClient
from ..schemas import AgentResponse, EvidencePack


@dataclass
class Responder:
    """Compose the final response using the collected evidence."""

    model_name: str = "responder"
    client: LLMClient | None = None

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = LLMClient(model_name=self.model_name)

    def respond(self, message: str, evidence_pack: EvidencePack) -> AgentResponse:
        """Return the final response payload for the caller."""

        citations = evidence_pack.citation_order()
        answer = self._compose_answer(message, evidence_pack)
        confidence = min(0.9, 0.3 + 0.1 * len(evidence_pack.items))
        unresolved = [] if evidence_pack.items else ["No supporting evidence collected."]
        return AgentResponse(
            answer=answer,
            citations=citations,
            confidence=confidence,
            unresolved_questions=unresolved,
        )

    def _compose_answer(self, message: str, pack: EvidencePack) -> str:
        lines: List[str] = [f"Question: {message}", "", "Findings:"]
        if not pack.items:
            lines.append("- No evidence available.")
        else:
            for item in pack.items:
                lines.append(f"- {item.content} [{item.id}]")
        return "\n".join(lines)


__all__ = ["Responder"]
