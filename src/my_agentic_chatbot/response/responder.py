"""Responder that assembles the final answer from evidence."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List

from ..config import get_settings
from ..llm_calls.llm_client import LLMClient, LLMMessage
from ..schemas import AgentResponse, EvidenceItem, EvidencePack

LOGGER = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "responder_system.md"


@dataclass
class Responder:
    """Compose the final response using the collected evidence."""

    model_name: str = "responder"
    client: LLMClient | None = None

    def __post_init__(self) -> None:
        if self.client is None:
            settings = get_settings()
            model_aliases = settings.model_aliases()
            target_model = model_aliases.get("responder", self.model_name)
            self.client = LLMClient(model_name=target_model)

    def respond(self, message: str, evidence_pack: EvidencePack) -> AgentResponse:
        """Return the final response payload for the caller."""

        owns_client = False
        responder_client = self.client
        if responder_client is None:
            owns_client = True
            settings = get_settings()
            target_model = settings.model_aliases().get("responder", self.model_name)
            responder_client = LLMClient(model_name=target_model)

        try:
            response_text = responder_client.chat(
                _build_messages(message, evidence_pack)
            )
        finally:
            if owns_client:
                responder_client.close()

        payload = _parse_responder_payload(response_text, evidence_pack)
        return AgentResponse.model_validate(payload)


def _build_messages(question: str, pack: EvidencePack) -> Iterable[LLMMessage]:
    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8").strip()
    evidence_lines = _render_evidence(pack.items)
    evidence_summary = pack.summary.strip()
    user_parts = [f"User question: {question}"]
    if evidence_summary:
        user_parts.append(f"Evidence summary: {evidence_summary}")
    user_parts.append("Evidence items:")
    user_parts.extend(evidence_lines or ["(no evidence collected)"])
    instructions = (
        "Return a JSON object with keys answer (string), citations (list[str]), "
        "confidence (float between 0 and 1), unresolved_questions (list[str])."
    )
    return [
        LLMMessage(role="system", content=f"{system_prompt}\n{instructions}"),
        LLMMessage(role="user", content="\n".join(user_parts)),
    ]


def _render_evidence(items: Iterable[EvidenceItem]) -> List[str]:
    lines: List[str] = []
    for item in items:
        snippet = item.content.replace("\n", " ").strip()
        metadata = []
        if item.source:
            metadata.append(f"source={item.source}")
        if item.metadata:
            metadata.append(
                ", ".join(f"{key}={value}" for key, value in sorted(item.metadata.items()))
            )
        meta_text = f" ({'; '.join(metadata)})" if metadata else ""
        lines.append(f"- {item.id}: {snippet}{meta_text}")
    return lines


def _parse_responder_payload(response: str, pack: EvidencePack) -> Dict[str, Any]:
    if not response:
        LOGGER.warning("Responder returned empty response, using fallback")
        return {
            "answer": "Unable to produce an answer with the available evidence.",
            "citations": [],
            "confidence": 0.0,
            "unresolved_questions": ["Responder returned an empty message."],
        }
    try:
        parsed = json.loads(response)
    except json.JSONDecodeError:
        LOGGER.debug("Responder response not pure JSON, attempting extraction")
        match = re.search(r"\{.*\}", response, re.DOTALL)
        if not match:
            LOGGER.error("Responder output missing JSON payload")
            return {
                "answer": response.strip(),
                "citations": pack.citation_order(),
                "confidence": 0.2,
                "unresolved_questions": ["Structured output missing; manual review required."],
            }
        parsed = json.loads(match.group())
    if not isinstance(parsed, dict):
        LOGGER.error("Responder JSON payload must be an object")
        return {
            "answer": response.strip(),
            "citations": pack.citation_order(),
            "confidence": 0.2,
            "unresolved_questions": ["Structured response malformed."],
        }
    parsed.setdefault("citations", pack.citation_order())
    parsed.setdefault("confidence", 0.5)
    parsed.setdefault("unresolved_questions", [])
    if not isinstance(parsed.get("citations", []), list):
        parsed["citations"] = pack.citation_order()
    return parsed


__all__ = ["Responder"]
