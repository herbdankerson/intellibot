"""Runtime helpers for executing custom LLM-backed agents."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..constants import MAX_EVIDENCE_ITEMS, MAX_SNIPPET_CHARS
from ..llm_calls.llm_client import LLMClient, LLMMessage
from ..schemas import EvidenceItem, PlanTask, Requirement
from ..util.text import build_snippet, deduplicate_items
from . import AgentConfig, AgentDescriptor

LOGGER = logging.getLogger(__name__)

_AGENT_PROMPT_PATH = (
    Path(__file__).resolve().parent / "prompts" / "custom_agent_system.md"
)


def _load_system_prompt() -> str:
    try:
        return _AGENT_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:  # pragma: no cover - defensive fallback
        return "You are a specialist assistant that produces compact evidence snippets."  # noqa: E501


@dataclass
class CustomAgentRunner:
    """Execute an LLM-backed custom agent task and produce evidence items."""

    descriptor: AgentDescriptor
    agent_config: AgentConfig
    client: Optional[LLMClient] = None

    def __post_init__(self) -> None:
        if self.client is None:
            model_key = self.agent_config.model
            target_model = model_key
            self.client = LLMClient(model_name=target_model)

    def execute(
        self,
        task: PlanTask,
        requirement: Requirement | None = None,
        **_: Any,
    ) -> List[EvidenceItem]:
        if self.client is None:  # pragma: no cover - defensive guard
            raise RuntimeError("Custom agent runner has no LLM client configured")

        messages = list(self._build_messages(task))
        raw_response = self.client.chat(messages, agent_config=self.agent_config)
        items = self._parse_response(raw_response, task)
        return deduplicate_items(items)[: MAX_EVIDENCE_ITEMS]

    def _build_messages(self, task: PlanTask) -> Iterable[LLMMessage]:
        system_prompt = _load_system_prompt()
        inputs = json.dumps(task.inputs, indent=2, ensure_ascii=False)
        user_lines = [
            f"Agent tool identifier: {task.tool}",
            f"Task description: {task.description}",
            f"Token budget: {task.budget_tokens} (strict)",
            f"Timeout: {task.timeout_seconds}s",
        ]
        if task.inputs:
            user_lines.append("Structured inputs:")
            user_lines.append(inputs)
        user_lines.append(
            (
                "Produce up to {limit} evidence snippets with concise sourcing. "
                "Return JSON with key 'items' (list) containing entries with fields "
                "id (optional), content, source, and metadata (object)."
            ).format(limit=MAX_EVIDENCE_ITEMS)
        )
        return [
            LLMMessage(role="system", content=system_prompt),
            LLMMessage(role="user", content="\n".join(user_lines)),
        ]

    def _parse_response(self, raw: str, task: PlanTask) -> List[EvidenceItem]:
        if not raw:
            LOGGER.warning("Custom agent %s returned empty response", task.tool)
            return []
        payload = self._extract_payload(raw)
        if isinstance(payload, dict):
            container = payload.get("items") or payload.get("evidence") or []
            if isinstance(container, list):
                records = container
            else:
                records = [payload]
        elif isinstance(payload, list):
            records = payload
        else:
            records = []

        evidence_items: List[EvidenceItem] = []
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                content = build_snippet(str(record), MAX_SNIPPET_CHARS)
                evidence_items.append(
                    self._build_item(task, index, content=content, source=None, metadata={})
                )
                continue
            evidence_items.append(self._build_item(task, index, **record))

        if not evidence_items:
            fallback_content = build_snippet(raw.strip(), MAX_SNIPPET_CHARS)
            evidence_items.append(
                self._build_item(task, 0, content=fallback_content, source=None, metadata={})
            )
        return evidence_items

    def _extract_payload(self, raw: str) -> Any:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}\s*$", raw, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    LOGGER.debug("Custom agent payload extraction failed", exc_info=True)
        return raw

    def _build_item(
        self,
        task: PlanTask,
        index: int,
        *,
        id: Optional[str] = None,
        content: Optional[str] = None,
        source: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        score: Optional[float] = None,
        **_unused: Any,
    ) -> EvidenceItem:
        snippet = build_snippet(content or "", MAX_SNIPPET_CHARS)
        item_id = id or f"{task.tool}-{index + 1}"
        item_source = source or f"agent:{self.descriptor.agent_config_name or task.tool}"
        meta: Dict[str, str] = {"agent": self.descriptor.agent_config_name or task.tool}
        if metadata:
            for key, value in metadata.items():
                if value is None:
                    continue
                meta[key] = str(value)
        if "tool" not in meta:
            meta["tool"] = task.tool
        if score is None:
            score_value = 0.0
        else:
            try:
                score_value = float(score)
            except (TypeError, ValueError):
                score_value = 0.0
        return EvidenceItem(
            id=item_id,
            source=item_source,
            content=snippet,
            score=score_value,
            metadata=meta,
        )


__all__ = ["CustomAgentRunner"]
