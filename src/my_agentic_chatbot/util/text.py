"""Utility helpers for working with text snippets."""

from __future__ import annotations

import re
from typing import Iterable, List, Tuple

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


__all__ = ["build_snippet", "deduplicate_items", "squeeze_whitespace"]
