"""Graph query helper wrapping the Neo4j MCP server."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List

from ..config import MCPServerConfig, get_settings
from ..mcp_client.mcp_client import MCPClient, MCPToolResponse
from ..schemas import EvidenceItem, PlanTask
from ..util.text import build_snippet

LOGGER = logging.getLogger(__name__)


@dataclass
class GraphTool:
    """Adapter that issues natural-language graph queries via MCP."""

    max_results: int = 3
    snippet_chars: int = 280
    client: MCPClient | None = None
    tool_name: str | None = None

    def __post_init__(self) -> None:
        if self.client is None:
            config = None
            settings = get_settings()
            for server_name in ("neo4j-cypher", "neo4j"):
                try:
                    config = settings.mcp_server(server_name)
                    break
                except KeyError:
                    continue
            if config is None:
                LOGGER.debug("Neo4j MCP config missing; GraphTool will operate in stub mode")
                return
            self.tool_name = self.tool_name or self._default_tool(config)
            self.client = MCPClient(
                base_url=config.url,
                token=config.token,
                server_name=f"{config.name}-client",
            )
        else:
            self.tool_name = self.tool_name or "graph_search"

    def execute(self, task: PlanTask) -> List[EvidenceItem]:
        """Execute the graph task, falling back to stub evidence if needed."""

        query = str(task.inputs.get("query") or task.description)
        if not query.strip():
            return []
        limit = self._resolve_limit(task)
        timeout = max(1, task.timeout_seconds)
        if not self.client or not self.tool_name:
            return self._stub_response(query)
        arguments = {
            "query": query,
            "limit": limit,
            "timeout_seconds": timeout,
        }
        response = self.client.call_tool_sync(self.tool_name, arguments)
        rows = self._extract_rows(response)
        if not rows:
            return self._stub_response(query)
        items = [self._row_to_evidence(index, row) for index, row in enumerate(rows[:limit])]
        return items or self._stub_response(query)

    def search(self, query: str) -> List[EvidenceItem]:
        """Compatibility helper for legacy tests; delegates to stub mode."""

        return self._stub_response(query)

    def _resolve_limit(self, task: PlanTask) -> int:
        if isinstance(task.inputs, dict):
            candidate = task.inputs.get("limit")
            if isinstance(candidate, int) and candidate > 0:
                return min(candidate, self.max_results)
        return self.max_results

    def _extract_rows(self, response: MCPToolResponse) -> List[Dict[str, Any]]:
        payload = response.best_effort_payload()
        if isinstance(payload, list):
            return [entry for entry in payload if isinstance(entry, dict)]
        if isinstance(payload, dict):
            items = payload.get("rows") or payload.get("items") or payload.get("paths")
            if isinstance(items, list):
                return [entry for entry in items if isinstance(entry, dict)]
        return []

    def _row_to_evidence(self, index: int, row: Dict[str, Any]) -> EvidenceItem:
        summary = row.get("summary") or row.get("text") or row.get("description") or ""
        snippet = build_snippet(str(summary), self.snippet_chars)
        locator = row.get("path") or row.get("relationship") or "neo4j"
        metadata: Dict[str, str] = {}
        for key, value in row.items():
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                continue
            if key in {"summary", "text", "description"}:
                continue
            metadata[key] = str(value)
        metadata.setdefault("retrieval_strategy", "graph-mcp")
        return EvidenceItem(
            id=f"graph-{index + 1}",
            source=str(locator),
            content=snippet,
            score=float(row.get("score", 0.3) or 0.3),
            metadata=metadata,
        )

    def _stub_response(self, query: str) -> List[EvidenceItem]:
        snippet = build_snippet(
            "Graph exploration requires approvals; this stub echoes the query for plumbing.",
            self.snippet_chars,
        )
        item = EvidenceItem(
            id="graph-1",
            source="neo4j-stub",
            content=f"{snippet} query={query}",
            score=0.2,
            metadata={"retrieval_strategy": "graph-stub"},
        )
        return [item]

    def _default_tool(self, config: MCPServerConfig) -> str:
        if config.tools:
            return config.tools[0]
        LOGGER.warning("Neo4j MCP config missing tools list; defaulting to graph_search")
        return "graph_search"


__all__ = ["GraphTool"]
