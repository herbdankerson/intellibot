"""Utility helpers for working with text snippets and evidence packs."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, Iterable, List, Sequence, Tuple

from ..schemas import EvidenceItem

_WHITESPACE_RE = re.compile(r"\s+")


def squeeze_whitespace(value: str) -> str:
    """Collapse consecutive whitespace to single spaces."""

    return _WHITESPACE_RE.sub(" ", value).strip()


def build_snippet(text: str, limit: int) -> str:
    """Trim a block of text to a maximum character length."""

    clean = squeeze_whitespace(text)
    if len(clean) <= limit:
        return clean
    truncated = clean[: max(0, limit - 1)].rstrip()
    return f"{truncated}…"


def deduplicate_items(items: Iterable[EvidenceItem]) -> List[EvidenceItem]:
    """Remove near-duplicate evidence items while preserving order."""

    seen: set[Tuple[str, str]] = set()
    unique: List[EvidenceItem] = []
    for item in items:
        fingerprint = (item.source, squeeze_whitespace(item.content))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(item)
    return unique


def reciprocal_rank_fuse(
    items: Sequence[EvidenceItem],
    *,
    ranking_key: str = "retrieval_strategy",
    k: int = 60,
) -> List[EvidenceItem]:
    """Apply Reciprocal Rank Fusion (RRF) to a sequence of evidence items."""

    if not items:
        return []

    grouped: Dict[str, List[EvidenceItem]] = defaultdict(list)
    for index, item in enumerate(items):
        strategy = item.metadata.get(ranking_key) if item.metadata else None
        if not strategy:
            strategy = item.source.split(":", 1)[0] if item.source else f"default:{index}"
        grouped[strategy].append(item)

    scores: Dict[str, float] = {}
    for group in grouped.values():
        sorted_group = sorted(
            group,
            key=lambda entry: (
                -float(entry.score if entry.score is not None else 0.0),
                squeeze_whitespace(entry.content),
            ),
        )
        for rank, entry in enumerate(sorted_group, start=1):
            scores[entry.id] = scores.get(entry.id, 0.0) + 1.0 / (k + rank)

    index_map = {item.id: idx for idx, item in enumerate(items)}
    sorted_items = sorted(
        items,
        key=lambda entry: (-scores.get(entry.id, 0.0), index_map.get(entry.id, 0)),
    )
    return sorted_items


def summarize_evidence(items: Sequence[EvidenceItem], *, max_items: int = 3) -> str:
    """Build a concise summary string for an evidence pack."""

    highlights: List[str] = []
    for item in items:
        snippet = squeeze_whitespace(item.content)
        if not snippet:
            continue
        highlights.append(f"{item.id}: {snippet}")
        if len(highlights) >= max_items:
            break
    return "\n".join(highlights)


def clip_to_token_budget(
    items: Sequence[EvidenceItem],
    *,
    token_budget: int,
) -> Tuple[List[EvidenceItem], bool]:
    """Trim evidence items so their combined token estimate fits within the budget."""

    if token_budget <= 0:
        return ([], bool(items))

    retained: List[EvidenceItem] = []
    running_tokens = 0
    truncated = False
    for item in items:
        estimate = item.token_estimate()
        if running_tokens + estimate > token_budget:
            truncated = True
            continue
        retained.append(item)
        running_tokens += estimate
    if not retained and items:
        retained = [items[0]]
        truncated = True
    return retained, truncated


__all__ = [
    "build_snippet",
    "clip_to_token_budget",
    "deduplicate_items",
    "reciprocal_rank_fuse",
    "summarize_evidence",
    "squeeze_whitespace",
]
