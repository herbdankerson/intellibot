"""Database search utilities used by the orchestrator."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

from ..config import MCPServerConfig, get_settings
from ..mcp_client.mcp_client import MCPClient, MCPToolResponse
from ..schemas import EvidenceItem
from ..util.text import build_snippet, deduplicate_items

LOGGER = logging.getLogger(__name__)


@dataclass
class DatabaseTool:
    """Adapter that queries the Postgres MCP server."""

    max_results: int = 5
    snippet_chars: int = 320
    client: MCPClient | None = None
    tool_name: str | None = None

    def __post_init__(self) -> None:
        if self.client is None:
            settings = get_settings()
            config = settings.mcp_server("postgres")
            self.tool_name = self.tool_name or self._default_tool(config)
            self.client = MCPClient(
                base_url=config.url,
                token=config.token,
                server_name=f"{config.name}-client",
            )
        else:
            self.tool_name = self.tool_name or "db_search"

    def search(self, query: str, *, limit: int | None = None) -> List[EvidenceItem]:
        """Return evidence items for the provided natural language query."""

        if not query.strip():
            return []
        limit = min(limit or self.max_results, self.max_results)
        if not self.client or not self.tool_name:
            raise RuntimeError("Database tool is not configured with an MCP client")

        response = self.client.call_tool_sync(
            self.tool_name,
            {"query": query, "limit": limit},
        )
        rows = self._extract_rows(response)
        items = [self._row_to_evidence(index, row) for index, row in enumerate(rows[:limit])]
        if not items and response.content:
            items = self._fallback_from_content(response.content, limit)
        return deduplicate_items(items)[:limit]

    def _default_tool(self, config: MCPServerConfig) -> str:
        if config.tools:
            return config.tools[0]
        LOGGER.warning("Postgres MCP config missing tools list; defaulting to db_search")
        return "db_search"

    def _extract_rows(self, response: MCPToolResponse) -> List[Dict[str, Any]]:
        payload = response.best_effort_payload()
        rows: List[Dict[str, Any]] = []
        if isinstance(payload, dict):
            for key in ("rows", "items", "results", "data"):
                candidate = payload.get(key)
                if isinstance(candidate, list):
                    rows = [entry for entry in candidate if isinstance(entry, dict)]
                    if rows:
                        break
        elif isinstance(payload, list):
            rows = [entry for entry in payload if isinstance(entry, dict)]
        else:
            LOGGER.debug("Unexpected MCP payload type", extra={"type": type(payload).__name__})
        return rows

    def _row_to_evidence(self, index: int, row: Dict[str, Any]) -> EvidenceItem:
        raw_content = row.get("snippet") or row.get("content") or row.get("text") or ""
        snippet = build_snippet(str(raw_content), self.snippet_chars)
        source = (
            row.get("source")
            or row.get("uri")
            or row.get("document_id")
            or row.get("id")
            or "postgres-mcp"
        )
        score_value = row.get("score") or row.get("rank") or row.get("bm25") or 0.0
        try:
            score = float(score_value)
        except (TypeError, ValueError):
            score = 0.0
        metadata: Dict[str, str] = {}
        for key, value in row.items():
            if key in {"snippet", "content", "text", "score", "rank", "bm25"}:
                continue
            if key.lower() in {"embedding", "vector", "embedding_vector"}:
                continue
            if value is None:
                continue
            metadata[key] = str(value)
        if "retrieval_strategy" not in metadata:
            metadata["retrieval_strategy"] = "postgres-mcp"
        return EvidenceItem(
            id=f"db-{index + 1}",
            source=str(source),
            content=snippet,
            score=score,
            metadata=metadata,
        )

    def _fallback_from_content(self, content: Iterable[str], limit: int) -> List[EvidenceItem]:
        items: List[EvidenceItem] = []
        for index, block in enumerate(content):
            if index >= limit:
                break
            snippet = build_snippet(str(block), self.snippet_chars)
            items.append(
                EvidenceItem(
                    id=f"db-{index + 1}",
                    source="postgres-mcp",
                    content=snippet,
                    score=0.0,
                    metadata={"retrieval_strategy": "text-fallback"},
                )
            )
        return items


__all__ = ["DatabaseTool"]
