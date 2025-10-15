"""Utility helpers for working with text snippets and evidence packs."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, Iterable, List, Sequence, Tuple

from ..schemas import EvidenceItem

_WHITESPACE_RE = re.compile(r"\s+")
_QUOTED_SUBJECT_RE = re.compile(r"[\"'](?P<subject>[^\"']{3,80}?)[\"']")
_TITLED_NAME_RE = re.compile(
    r"\b(?P<title>(?i:judge|justice|magistrate))\s+(?P<name>(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}))"
)
_LOCATION_FOLLOW_RE = re.compile(
    r"\b(?:of|in|from|at)\s+(?P<location>[A-Z][^,.;\n]{2,80})",
    re.IGNORECASE,
)
_LOCATION_STOP_RE = re.compile(
    r"\b(?:and|with|whose|who|which|covering|focusing|including|seeking|supplementing|providing|for)\b",
    re.IGNORECASE,
)


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


def extract_subject_and_location(text: str) -> Tuple[str | None, str | None]:
    """Heuristically extract a named subject and optional location from text."""

    if not text:
        return (None, None)

    subject: str | None = None
    span_end = 0

    quoted_matches = list(_QUOTED_SUBJECT_RE.finditer(text))
    if quoted_matches:
        best = max(quoted_matches, key=lambda match: len(match.group("subject")))
        subject_candidate = best.group("subject").strip()
        if subject_candidate:
            subject = subject_candidate
            span_end = best.end()
    else:
        titled_matches = list(_TITLED_NAME_RE.finditer(text))
        if titled_matches:
            best = max(titled_matches, key=lambda match: len(match.group("name")))
            title = best.group("title").strip()
            name = best.group("name").strip()
            if title and name:
                subject = f"{title.title()} {name}"
                span_end = best.end()

    if subject is None:
        return (None, None)

    trailing = text[span_end : span_end + 240]
    location_match = _LOCATION_FOLLOW_RE.search(trailing)
    if not location_match:
        return (subject, None)

    location = location_match.group("location").strip()
    remainder = trailing[location_match.end() : location_match.end() + 40]
    if remainder.startswith(","):
        state_match = re.match(r",\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", remainder)
        if state_match:
            location = f"{location} {state_match.group(1)}"
    # Truncate when we hit stop words or punctuation that likely indicates new clause.
    stop_match = _LOCATION_STOP_RE.search(location)
    if stop_match:
        location = location[: stop_match.start()].strip()
    location = location.split(".")[0].split(";")[0]
    location = location.replace("\n", " ")
    location = squeeze_whitespace(location)
    if not location:
        return (subject, None)
    return (subject, location.rstrip(","))


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
    "extract_subject_and_location",
    "reciprocal_rank_fuse",
    "summarize_evidence",
    "squeeze_whitespace",
]
