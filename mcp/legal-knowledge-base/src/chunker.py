import re
from typing import Dict, List

from nltk.tokenize import sent_tokenize


def rough_tokens(text: str) -> int:
    return max(1, int(len(text) / 4))


def split_by_md_headings(markdown: str) -> List[Dict]:
    parts = re.split(r"(?m)^(#{1,6}\s.*)$", markdown)
    sections: List[Dict] = []
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
    if token_len <= max_tokens:
        return 0
    if token_len <= 2 * max_tokens:
        return int(0.10 * max_tokens)
    return int(overlap_max_pct * max_tokens)


def sentence_chunk(text: str, max_tokens: int, overlap_tokens: int) -> List[str]:
    sentences = sent_tokenize(text)
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


def build_chunks(markdown: str, max_tokens: int, overlap_max_pct: float) -> List[Dict]:
    output: List[Dict] = []
    for section in split_by_md_headings(markdown):
        token_len = rough_tokens(section["text"])
        overlap = decide_overlap(token_len, max_tokens, overlap_max_pct)
        if token_len <= max_tokens:
            output.append(
                {
                    "title": section["title"],
                    "text": section["text"],
                    "idx": 0,
                    "token_count": token_len,
                    "overlap": 0,
                }
            )
            continue
        pieces = sentence_chunk(section["text"], max_tokens, overlap)
        for idx, chunk_text in enumerate(pieces):
            output.append(
                {
                    "title": section["title"],
                    "text": chunk_text,
                    "idx": idx,
                    "token_count": rough_tokens(chunk_text),
                    "overlap": overlap,
                }
            )
    return output

