"""Database search utilities used by the orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Sequence

from ..schemas import EvidenceItem
from ..util.text import build_snippet, deduplicate_items


@dataclass
class DatabaseTool:
    """Very small in-memory database search helper."""

    max_results: int = 5
    snippet_chars: int = 320
    documents: Sequence[Dict[str, str]] = field(default_factory=lambda: list(_default_documents()))

    def search(self, query: str, *, limit: int | None = None) -> List[EvidenceItem]:
        """Return evidence items for the provided natural language query."""

        limit = limit or self.max_results
        limit = min(limit, self.max_results)
        scored_docs = sorted(
            self._scored_documents(query=query),
            key=lambda entry: entry["score"],
            reverse=True,
        )
        items: List[EvidenceItem] = []
        for index, doc in enumerate(scored_docs[:limit]):
            snippet = build_snippet(doc["content"], self.snippet_chars)
            item = EvidenceItem(
                id=f"db-{index + 1}",
                source=doc["source"],
                content=snippet,
                score=doc["score"],
                metadata={
                    "document_id": doc["id"],
                    "title": doc.get("title", ""),
                    "retrieval_strategy": "bm25-lite",
                },
            )
            items.append(item)
        return deduplicate_items(items)

    def _scored_documents(self, query: str) -> Iterable[Dict[str, str]]:
        """Yield documents with a very small relevance score."""

        normalized_query = query.lower().split()
        for doc in self.documents:
            corpus = doc["content"].lower()
            score = sum(1 for token in normalized_query if token in corpus)
            yield {**doc, "score": float(score or 0.1)}


def _default_documents() -> Iterable[Dict[str, str]]:
    """Return a deterministic, in-memory corpus for local tests."""

    yield {
        "id": "doc-1",
        "title": "Agentic retrieval overview",
        "source": "kb_documents:1",
        "content": (
            "Agentic retrieval pipelines combine planners, workflow orchestrators, "
            "and tool agents. The database stage blends BM25 and vector search to "
            "produce compact snippets with strong recall before handing to a "
            "responder model."
        ),
    }
    yield {
        "id": "doc-2",
        "title": "Prefect orchestration",
        "source": "kb_documents:2",
        "content": (
            "Prefect flows coordinate approvals and retries. Each task receives a "
            "strict timeout and token budget so that evidence packs stay within the "
            "global limit."
        ),
    }
    yield {
        "id": "doc-3",
        "title": "Evidence packs",
        "source": "kb_documents:3",
        "content": (
            "Evidence packs deduplicate overlapping passages and trim each snippet "
            "to a few hundred characters. Citations reference the evidence item id "
            "and unresolved assumptions are recorded."
        ),
    }


__all__ = ["DatabaseTool"]
