"""Centralized workflow policies such as budgets and limits."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List

from ..schemas import EvidenceItem, EvidencePack

DEFAULT_DB_BUDGET_TOKENS = 600
DEFAULT_DB_TIMEOUT_SECONDS = 30
DEFAULT_GRAPH_BUDGET_TOKENS = 800
DEFAULT_GRAPH_TIMEOUT_SECONDS = 45
DEFAULT_WEB_BUDGET_TOKENS = 400
DEFAULT_WEB_TIMEOUT_SECONDS = 20

MAX_EVIDENCE_ITEMS = 6
MAX_SNIPPET_CHARS = 320
EVIDENCE_TOKEN_BUDGET = 1200


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


__all__ = [
    "DEFAULT_DB_BUDGET_TOKENS",
    "DEFAULT_DB_TIMEOUT_SECONDS",
    "DEFAULT_GRAPH_BUDGET_TOKENS",
    "DEFAULT_GRAPH_TIMEOUT_SECONDS",
    "DEFAULT_WEB_BUDGET_TOKENS",
    "DEFAULT_WEB_TIMEOUT_SECONDS",
    "EVIDENCE_TOKEN_BUDGET",
    "MAX_EVIDENCE_ITEMS",
    "MAX_SNIPPET_CHARS",
    "PolicyDecision",
    "clamp_evidence",
    "within_token_budget",
]
