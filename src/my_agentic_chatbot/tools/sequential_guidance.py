"""Lightweight sequential guidance helper for web search refinement."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List

from ..config import get_settings
from ..llm_calls.llm_client import LLMClient, LLMMessage

LOGGER = logging.getLogger(__name__)


@dataclass
class GuidanceResult:
    """Structured guidance returned by the sequential analyser."""

    notes: List[str] = field(default_factory=list)
    queries: List[str] = field(default_factory=list)
    priority_urls: List[str] = field(default_factory=list)

    def as_note(self) -> str:
        parts: List[str] = []
        if self.notes:
            parts.append("notes: " + " | ".join(self.notes))
        if self.queries:
            parts.append("follow-ups: " + "; ".join(self.queries))
        if self.priority_urls:
            parts.append("priority urls: " + "; ".join(self.priority_urls))
        return "sequential guidance: " + " / ".join(parts) if parts else ""


class SequentialGuidance:
    """Deterministic guidance module that proposes follow-up web actions."""

    def __init__(self, *, model: str | None = None, max_queries: int = 3) -> None:
        settings = get_settings()
        self.model = model or settings.cheap_worker_model
        self.max_queries = max_queries

    def analyse(
        self,
        requirement: str,
        query: str,
        candidates: Iterable[Dict[str, Any]],
    ) -> GuidanceResult:
        """Return follow-up suggestions using the configured LLM."""

        rows = list(candidates)
        if not rows:
            return GuidanceResult()

        summary_lines = []
        for index, row in enumerate(rows, start=1):
            title = str(row.get("title") or row.get("url") or row.get("source") or f"candidate {index}")
            snippet = str(row.get("snippet") or row.get("content") or "").strip()
            url = str(row.get("url") or row.get("source") or "").strip()
            summary_lines.append(
                {
                    "index": index,
                    "title": title[:200],
                    "snippet": snippet[:400],
                    "url": url[:400],
                }
            )

        prompt_payload = {
            "requirement": requirement,
            "initial_query": query,
            "candidates": summary_lines,
            "instructions": (
                "Analyse the results, decide if the requirement is satisfied, "
                "and propose up to three concise follow-up web queries if gaps remain. "
                "Return strict JSON with keys: notes (list of strings), queries (list of strings), "
                "priority_urls (list of strings)."),
        }

        messages = [
            LLMMessage(
                role="system",
                content=(
                    "You are a senior research planner supervising web evidence collection. "
                    "Inspect the candidate list, identify coverage gaps, and suggest targeted follow-ups. "
                    "Only return JSON."
                ),
            ),
            LLMMessage(role="user", content=json.dumps(prompt_payload, ensure_ascii=False)),
        ]

        client = LLMClient(model_name=self.model, temperature=0.2)
        try:
            response = client.chat(messages, max_tokens=600)
        finally:
            client.close()

        return _parse_guidance_response(response, self.max_queries)


def _parse_guidance_response(payload: str, max_queries: int) -> GuidanceResult:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        LOGGER.debug("Sequential guidance returned non-JSON payload", extra={"payload": payload})
        return GuidanceResult(notes=[payload.strip()[:320]]) if payload.strip() else GuidanceResult()

    notes = _coerce_str_list(data.get("notes"))
    queries = _coerce_str_list(data.get("queries"))[:max_queries]
    priority_urls = _coerce_str_list(data.get("priority_urls"))[:max_queries]
    return GuidanceResult(notes=notes, queries=queries, priority_urls=priority_urls)


def _coerce_str_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(entry).strip() for entry in value if str(entry).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


__all__ = ["GuidanceResult", "SequentialGuidance"]
