"""Responder that assembles the final answer from evidence."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

from ..agents import AgentConfig, get_agent_config
from ..config import get_settings
from ..llm_calls.llm_client import LLMClient, LLMMessage
from ..schemas import AgentResponse, EvidenceItem, EvidencePack, Finding, OpenQuestion

LOGGER = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "responder_system.md"


@dataclass
class Responder:
    """Compose the final response using the collected evidence."""

    model_name: str = "responder"
    client: LLMClient | None = None
    agent_config: AgentConfig | None = None

    def __post_init__(self) -> None:
        settings = get_settings()
        if self.agent_config is None:
            self.agent_config = get_agent_config("responder")
        model_aliases = settings.model_aliases()
        model_key = self.agent_config.model if self.agent_config else self.model_name
        target_model = model_aliases.get(model_key, model_key)
        if self.client is None:
            self.client = LLMClient(model_name=target_model)

    def respond(
        self,
        message: str,
        evidence_pack: EvidencePack,
        *,
        findings: Sequence[Finding],
        acceptance_criteria: Sequence[str],
        open_questions: Sequence[OpenQuestion] | None = None,
    ) -> AgentResponse:
        """Return the final response payload for the caller."""

        agent_config = self.agent_config or get_agent_config("responder")
        if self.agent_config is None:
            self.agent_config = agent_config

        owns_client = False
        responder_client = self.client
        if responder_client is None:
            owns_client = True
            settings = get_settings()
            model_aliases = settings.model_aliases()
            model_key = agent_config.model if agent_config else self.model_name
            target_model = model_aliases.get(model_key, model_key)
            responder_client = LLMClient(model_name=target_model)

        try:
            messages = _build_messages(
                message,
                evidence_pack,
                findings,
                acceptance_criteria,
                open_questions or [],
            )
            if isinstance(responder_client, LLMClient):
                response_text = responder_client.chat(
                    messages,
                    agent_config=agent_config,
                )
            else:  # accommodate test doubles without keyword support
                response_text = responder_client.chat(messages)
        finally:
            if owns_client:
                responder_client.close()

        payload = _parse_responder_payload(response_text, evidence_pack)
        return AgentResponse.model_validate(payload)


def _build_messages(
    question: str,
    pack: EvidencePack,
    findings: Sequence[Finding],
    acceptance: Sequence[str],
    open_questions: Sequence[OpenQuestion],
) -> Iterable[LLMMessage]:
    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8").strip()
    evidence_lines = _render_evidence(pack.items)
    finding_lines = _render_findings(findings)
    acceptance_lines = [f"- {item}" for item in acceptance] if acceptance else []
    open_question_lines = _render_open_questions(open_questions)

    instructions = (
        "Respond ONLY with claims supported by the provided findings. "
        "Every sentence containing a claim must cite one or more evidence IDs. "
        "If acceptance criteria cannot be satisfied, explain the gap and recommend next steps."
    )

    user_sections: List[str] = [f"User question: {question}"]
    if acceptance_lines:
        user_sections.append("Acceptance criteria:\n" + "\n".join(acceptance_lines))
    if finding_lines:
        user_sections.append("Findings (must be respected):\n" + "\n".join(finding_lines))
    else:
        user_sections.append("Findings: none (answer cautiously)")
    if open_question_lines:
        user_sections.append("Outstanding gaps to address if possible:\n" + "\n".join(open_question_lines))
    evidence_summary = pack.summary.strip()
    if evidence_summary:
        user_sections.append(f"Evidence summary: {evidence_summary}")
    user_sections.append("Evidence items:")
    user_sections.extend(evidence_lines or ["(no evidence collected)"])
    user_sections.append(
        "Return a JSON object with keys answer (string), citations (list[str]), "
        "confidence (float between 0 and 1), unresolved_questions (list[str])."
    )

    return [
        LLMMessage(role="system", content=f"{system_prompt}\n{instructions}"),
        LLMMessage(role="user", content="\n\n".join(user_sections)),
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


def _render_findings(findings: Sequence[Finding]) -> List[str]:
    lines: List[str] = []
    for finding in findings:
        evidence_refs = ", ".join(finding.evidence_ids) if finding.evidence_ids else ""
        meta = f" [confidence={finding.confidence:.2f}]"
        if evidence_refs:
            meta += f" (evidence: {evidence_refs})"
        lines.append(f"- {finding.id}: {finding.value}{meta}")
    return lines


def _render_open_questions(questions: Sequence[OpenQuestion]) -> List[str]:
    lines: List[str] = []
    for item in questions:
        reason = f" — {item.reason}" if item.reason else ""
        requirement = f" [requirement={item.requirement_id}]" if item.requirement_id else ""
        lines.append(f"- {item.question}{reason}{requirement}")
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
    valid_ids = set(pack.citation_order())
    parsed.setdefault("citations", pack.citation_order())
    parsed.setdefault("confidence", 0.5)
    parsed.setdefault("unresolved_questions", [])
    if not isinstance(parsed.get("citations", []), list):
        parsed["citations"] = pack.citation_order()
    else:
        parsed["citations"] = [
            citation
            for citation in parsed["citations"]
            if isinstance(citation, str) and citation in valid_ids
        ]
        if not parsed["citations"] and valid_ids:
            parsed["citations"] = list(valid_ids)
    return parsed


__all__ = ["Responder"]
