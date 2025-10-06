"""Centralized workflow policies such as budgets and limits."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, List

from ..agents import get_agent_catalog
from ..config import get_settings
from ..constants import (
    DEFAULT_CUSTOM_AGENT_TIMEOUT_SECONDS,
    DEFAULT_DB_BUDGET_TOKENS,
    DEFAULT_DB_TIMEOUT_SECONDS,
    DEFAULT_GRAPH_BUDGET_TOKENS,
    DEFAULT_GRAPH_TIMEOUT_SECONDS,
    DEFAULT_WEB_BUDGET_TOKENS,
    DEFAULT_WEB_TIMEOUT_SECONDS,
    EVIDENCE_TOKEN_BUDGET,
    MAX_EVIDENCE_ITEMS,
    MAX_SNIPPET_CHARS,
    MAX_WEB_RESULTS,
)
from ..schemas import EvidenceItem, EvidencePack, PlanTask

DEFAULT_TOOL_TIMEOUT_SECONDS = DEFAULT_CUSTOM_AGENT_TIMEOUT_SECONDS
TOOL_TIMEOUT_LIMITS = {
    "db_search": DEFAULT_DB_TIMEOUT_SECONDS,
    "graph_search": DEFAULT_GRAPH_TIMEOUT_SECONDS,
    "web_search": DEFAULT_WEB_TIMEOUT_SECONDS,
}


def clamp_evidence(items: Iterable[EvidenceItem]) -> List[EvidenceItem]:
    """Truncate evidence to respect global policies."""

    limited_items: List[EvidenceItem] = []
    for item in items:
        if len(limited_items) >= MAX_EVIDENCE_ITEMS:
            break
        limited_items.append(item)
    return limited_items


def within_token_budget(pack: EvidencePack) -> bool:
    """Return True if the evidence pack respects the token budget."""

    return pack.token_count <= EVIDENCE_TOKEN_BUDGET


@dataclass
class PolicyDecision:
    """Outcome of a policy evaluation."""

    allowed: bool
    reason: str = ""


def enforce_task_limits(task: PlanTask) -> PolicyDecision:
    """Ensure the task obeys timeout and limit policies."""

    limit = _timeout_limit_for(task)
    if task.timeout_seconds > limit:
        return PolicyDecision(
            allowed=False,
            reason=f"timeout {task.timeout_seconds}s exceeds {limit}s limit",
        )
    requested_limit = _normalize_limit_input(
        task.inputs.get("limit") if isinstance(task.inputs, dict) else None
    )
    if requested_limit is not None and requested_limit > MAX_EVIDENCE_ITEMS:
        return PolicyDecision(
            allowed=False,
            reason=f"row limit {requested_limit} exceeds max {MAX_EVIDENCE_ITEMS}",
        )
    return PolicyDecision(allowed=True)


def _normalize_limit_input(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _timeout_limit_for(task: PlanTask) -> int:
    overrides = get_settings().tool_timeout_overrides or {}
    if task.tool in overrides:
        try:
            override = int(overrides[task.tool])
            if override > 0:
                return override
        except (TypeError, ValueError):  # pragma: no cover - defensive
            logging.getLogger(__name__).warning(
                "Invalid timeout override for tool",
                extra={"tool": task.tool, "value": overrides[task.tool]},
            )
    descriptor = get_agent_catalog().get(task.tool)
    if descriptor:
        return max(1, descriptor.default_timeout_seconds)
    return TOOL_TIMEOUT_LIMITS.get(task.tool, DEFAULT_TOOL_TIMEOUT_SECONDS)


__all__ = [
    "DEFAULT_DB_BUDGET_TOKENS",
    "DEFAULT_DB_TIMEOUT_SECONDS",
    "DEFAULT_GRAPH_BUDGET_TOKENS",
    "DEFAULT_GRAPH_TIMEOUT_SECONDS",
    "DEFAULT_WEB_BUDGET_TOKENS",
    "DEFAULT_WEB_TIMEOUT_SECONDS",
    "EVIDENCE_TOKEN_BUDGET",
    "DEFAULT_TOOL_TIMEOUT_SECONDS",
    "MAX_EVIDENCE_ITEMS",
    "MAX_WEB_RESULTS",
    "MAX_SNIPPET_CHARS",
    "TOOL_TIMEOUT_LIMITS",
    "PolicyDecision",
    "clamp_evidence",
    "enforce_task_limits",
    "within_token_budget",
]
