"""Audit agent that validates responder output prior to delivery."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ..agents import AgentConfig, get_agent_config
from ..config import get_settings
from ..llm_calls.llm_client import LLMClient, LLMMessage
from ..schemas import AgentResponse, AuditFinding, AuditReport, EvidenceItem, EvidencePack, Plan
from ..util.text import squeeze_whitespace

LOGGER = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "audit_system.md"


@dataclass
class AuditAgent:
    """LLM-backed audit stage ensuring responses meet quality gates."""

    model_name: str = "audit"
    client: Optional[LLMClient] = None
    agent_config: Optional[AgentConfig] = None

    def __post_init__(self) -> None:
        self._enabled = True
        if self.agent_config is None:
            try:
                self.agent_config = get_agent_config("audit")
            except FileNotFoundError:
                LOGGER.warning("Audit agent configuration not found; disabling audit checks")
                self._enabled = False
                return
        if self.client is None and self._enabled:
            settings = get_settings()
            model_aliases = settings.model_aliases()
            model_key = self.agent_config.model if self.agent_config else self.model_name
            target_model = model_aliases.get(model_key, model_key)
            self.client = LLMClient(model_name=target_model)

    def evaluate(
        self,
        *,
        question: str,
        plan: Plan,
        evidence: EvidencePack,
        response: AgentResponse,
    ) -> AuditReport:
        if not self._enabled or self.client is None or self.agent_config is None:
            return AuditReport(
                passed=True,
                summary="Audit disabled",
                coverage={criterion: True for criterion in plan.acceptance_criteria},
                findings=[],
            )

        messages = list(
            self._build_messages(
                question=question,
                plan=plan,
                evidence=evidence,
                response=response,
            )
        )
        try:
            raw_reply = self.client.chat(messages, agent_config=self.agent_config)
        except Exception:  # pragma: no cover - defensive guard
            LOGGER.exception("Audit agent call failed")
            return AuditReport(
                passed=False,
                summary="Audit agent call failed",
                coverage={criterion: False for criterion in plan.acceptance_criteria},
                findings=[
                    AuditFinding(
                        severity="error",
                        message="Audit agent call raised an exception",
                        citations=[],
                        acceptance_criteria=[],
                    )
                ],
            )
        return self._parse_response(raw_reply, plan)

    def _build_messages(
        self,
        *,
        question: str,
        plan: Plan,
        evidence: EvidencePack,
        response: AgentResponse,
    ) -> Iterable[LLMMessage]:
        system_prompt = _PROMPT_PATH.read_text(encoding="utf-8").strip()
        acceptance_lines = plan.acceptance_criteria or ["Answer must cite supporting evidence."]
        assumption_lines = plan.assumptions or []
        evidence_lines = self._render_evidence(evidence.items)
        user_sections: List[str] = [
            f"User question: {question}",
            "Acceptance criteria:",
            *[f"- {line}" for line in acceptance_lines],
        ]
        if assumption_lines:
            user_sections.extend(["Assumptions:", *[f"- {line}" for line in assumption_lines]])
        user_sections.extend(
            [
                "Responder answer:",
                squeeze_whitespace(response.answer),
                f"Citations used: {', '.join(response.citations) or '(none)'}",
                f"Confidence: {response.confidence}",
                f"Unresolved questions: {', '.join(response.unresolved_questions) or '(none)'}",
            ]
        )
        if evidence.summary:
            user_sections.append(f"Evidence summary: {squeeze_whitespace(evidence.summary)}")
        user_sections.append("Evidence items:")
        user_sections.extend(evidence_lines or ["(no evidence collected)"])
        instructions = (
            "Return JSON with keys: passed (bool), summary (string), coverage (object mapping "
            "acceptance criteria to booleans), and findings (list). Each finding must include "
            "severity ('info'|'warning'|'error'), message, citations (list of evidence ids), "
            "and acceptance_criteria (list)."
        )
        return [
            LLMMessage(role="system", content=f"{system_prompt}\n{instructions}"),
            LLMMessage(role="user", content="\n".join(user_sections)),
        ]

    def _render_evidence(self, items: Iterable[EvidenceItem]) -> List[str]:
        lines: List[str] = []
        for item in items:
            snippet = squeeze_whitespace(item.content)
            if not snippet:
                continue
            metadata_bits = []
            if item.source:
                metadata_bits.append(f"source={item.source}")
            if item.metadata:
                sorted_meta = ", ".join(f"{k}={v}" for k, v in sorted(item.metadata.items()))
                metadata_bits.append(sorted_meta)
            meta_text = f" ({'; '.join(metadata_bits)})" if metadata_bits else ""
            lines.append(f"- {item.id}: {snippet}{meta_text}")
        return lines

    def _parse_response(self, raw: str, plan: Plan) -> AuditReport:
        if not raw:
            return AuditReport(
                passed=False,
                summary="Audit agent returned empty response",
                coverage={criterion: False for criterion in plan.acceptance_criteria},
                findings=[
                    AuditFinding(
                        severity="error",
                        message="Audit agent returned no content",
                        citations=[],
                        acceptance_criteria=[],
                    )
                ],
            )
        payload = self._extract_json(raw)
        if not isinstance(payload, dict):
            return AuditReport(
                passed=False,
                summary="Audit agent response malformed",
                coverage={criterion: False for criterion in plan.acceptance_criteria},
                findings=[
                    AuditFinding(
                        severity="error",
                        message="Audit output missing top-level object",
                        citations=[],
                        acceptance_criteria=[],
                    )
                ],
            )
        passed = bool(payload.get("passed", False))
        summary = squeeze_whitespace(str(payload.get("summary", "")))
        coverage_raw = payload.get("coverage", {})
        coverage: Dict[str, bool] = {}
        if isinstance(coverage_raw, dict):
            for key, value in coverage_raw.items():
                coverage[str(key)] = bool(value)
        else:
            coverage = {criterion: False for criterion in plan.acceptance_criteria}
        findings_data = payload.get("findings", [])
        findings: List[AuditFinding] = []
        if isinstance(findings_data, list):
            for entry in findings_data:
                if not isinstance(entry, dict):
                    continue
                severity = str(entry.get("severity", "info")).lower()
                if severity not in {"info", "warning", "error"}:
                    severity = "info"
                message = squeeze_whitespace(str(entry.get("message", "")))
                citations_field = entry.get("citations", [])
                acceptance_field = entry.get("acceptance_criteria", [])
                citations = [str(item) for item in citations_field if str(item)] if isinstance(citations_field, list) else []
                acceptance = [str(item) for item in acceptance_field if str(item)] if isinstance(acceptance_field, list) else []
                findings.append(
                    AuditFinding(
                        severity=severity, message=message, citations=citations, acceptance_criteria=acceptance
                    )
                )
        if not findings:
            findings = [AuditFinding(severity="info", message="No issues detected", citations=[], acceptance_criteria=[])]
        if not summary:
            summary = findings[0].message
        return AuditReport(passed=passed, summary=summary, coverage=coverage, findings=findings)

    def _extract_json(self, raw: str):  # type: ignore[override]
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    LOGGER.debug("Audit agent JSON extraction failed", exc_info=True)
        return {}


__all__ = ["AuditAgent"]
