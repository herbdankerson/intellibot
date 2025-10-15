"""Chunking utilities shared by the ingestion pipeline.

The functions in this module are largely ported from the Legal Knowledge MCP
server so that we maintain identical chunking behaviour across ingestion
surfaces.
"""

from __future__ import annotations

import re
from typing import Dict, List

try:
    from nltk.tokenize import sent_tokenize as _nltk_sent_tokenize
except ImportError:  # pragma: no cover - nltk is an optional dependency at runtime
    _nltk_sent_tokenize = None  # type: ignore[assignment]


def rough_tokens(text: str) -> int:
    """Approximate the token count using a simple heuristic."""

    return max(1, int(len(text) / 4))


def _sent_tokenize(text: str) -> List[str]:
    """Tokenize text into sentences with a graceful fallback."""

    if not text:
        return []
    if _nltk_sent_tokenize is not None:
        try:
            return _nltk_sent_tokenize(text)
        except LookupError:  # NLTK data not downloaded
            pass
    # Fallback: split on sentence-ending punctuation.
    return [segment for segment in re.split(r"(?<=[.!?])\s+", text) if segment]


def split_by_md_headings(markdown: str) -> List[Dict[str, str]]:
    """Split a markdown document into sections keyed by headings."""

    parts = re.split(r"(?m)^(#{1,6}\s.*)$", markdown)
    sections: List[Dict[str, str]] = []
    heading = "Document"
    idx = 0
    while idx < len(parts):
        piece = parts[idx]
        if re.match(r"^#{1,6}\s", piece or ""):
            heading = piece.lstrip("# ").strip() or heading
            idx += 1
            continue
        body = (piece or "").strip()
        if body:
            sections.append({"title": heading, "text": body})
        idx += 1
    return sections or [{"title": "Document", "text": markdown}]


def decide_overlap(token_len: int, max_tokens: int, overlap_max_pct: float) -> int:
    """Determine the appropriate overlap tokens for a section."""

    if token_len <= max_tokens:
        return 0
    if token_len <= 2 * max_tokens:
        return int(0.10 * max_tokens)
    return int(overlap_max_pct * max_tokens)


def sentence_chunk(text: str, max_tokens: int, overlap_tokens: int) -> List[str]:
    """Split text into chunks based on sentence boundaries."""

    sentences = _sent_tokenize(text)
    chunks: List[str] = []
    buffer: List[str] = []
    buffer_tokens = 0
    for sentence in sentences:
        t = rough_tokens(sentence)
        if buffer and buffer_tokens + t > max_tokens:
            chunks.append(" ".join(buffer))
            if overlap_tokens > 0:
                tail: List[str] = []
                tokens = 0
                for sent in reversed(buffer):
                    tokens += rough_tokens(sent)
                    tail.append(sent)
                    if tokens >= overlap_tokens:
                        break
                buffer = list(reversed(tail))
                buffer_tokens = sum(rough_tokens(item) for item in buffer)
            else:
                buffer = []
                buffer_tokens = 0
        buffer.append(sentence)
        buffer_tokens += t
    if buffer:
        chunks.append(" ".join(buffer))
    return chunks


def build_chunks(markdown: str, max_tokens: int, overlap_max_pct: float) -> List[Dict[str, object]]:
    """Generate chunk dictionaries mirroring the MCP server implementation."""

    output: List[Dict[str, object]] = []
    next_idx = 0
    for section in split_by_md_headings(markdown):
        token_len = rough_tokens(section["text"])
        overlap = decide_overlap(token_len, max_tokens, overlap_max_pct)
        if token_len <= max_tokens:
            output.append(
                {
                    "title": section["title"],
                    "text": section["text"],
                    "idx": next_idx,
                    "token_count": token_len,
                    "overlap": 0,
                }
            )
            next_idx += 1
            continue
        pieces = sentence_chunk(section["text"], max_tokens, overlap)
        for chunk_text in pieces:
            output.append(
                {
                    "title": section["title"],
                    "text": chunk_text,
                    "idx": next_idx,
                    "token_count": rough_tokens(chunk_text),
                    "overlap": overlap,
                }
            )
            next_idx += 1
    return output
