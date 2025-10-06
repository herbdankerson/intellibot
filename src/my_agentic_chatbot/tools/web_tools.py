"""Web search helper backed by the SearxNG MCP server."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List

from ..config import MCPServerConfig, get_settings
from ..mcp_client.mcp_client import MCPClient, MCPToolResponse
from ..schemas import EvidenceItem, PlanTask
from ..util.text import build_snippet, deduplicate_items

LOGGER = logging.getLogger(__name__)


@dataclass
class WebTool:
    """Adapter that federates SearxNG search results through MCP."""

    max_results: int = 4
    snippet_chars: int = 240
    client: MCPClient | None = None
    tool_name: str | None = None
    profiles: Dict[str, Dict[str, Any]] = field(
        default_factory=lambda: {
            "quick": {"engines": ["google"], "num_pages": 1},
            "deep": {"engines": ["google", "bing", "duckduckgo"], "num_pages": 2},
            "code": {"engines": ["github", "stack_overflow"], "categories": ["it"]},
            "legal": {"engines": ["law_arxiv", "courtlistener"], "categories": ["law"]},
            "academic": {"engines": ["semantic_scholar", "arxiv"], "categories": ["science"]},
        }
    )

    def __post_init__(self) -> None:
        if self.client is None:
            try:
                config = get_settings().mcp_server("web")
            except KeyError:
                LOGGER.debug("Web MCP config missing; WebTool will operate in stub mode")
                return
            self.tool_name = self.tool_name or self._default_tool(config)
            self.client = MCPClient(
                base_url=config.url,
                token=config.token,
                server_name=f"{config.name}-client",
            )
        else:
            self.tool_name = self.tool_name or "web_search"

    def execute(self, task: PlanTask, *, limit: int | None = None) -> List[EvidenceItem]:
        """Execute a search task using MCP, falling back to stubbed evidence."""

        query = str(task.inputs.get("query") or task.description)
        if not query.strip():
            return []
        limit = self._resolve_limit(task, limit)
        timeout = max(1, task.timeout_seconds)
        if not self.client or not self.tool_name:
            return self._stub_response(query, limit)
        arguments = self._build_arguments(task, query, limit, timeout)
        response = self.client.call_tool_sync(self.tool_name, arguments)
        items = self._parse_response(response, limit, arguments)
        return items or self._stub_response(query, limit)

    def search(self, query: str) -> List[EvidenceItem]:
        """Legacy helper used by tests; returns stub evidence."""

        return self._stub_response(query, self.max_results)

    def _resolve_limit(self, task: PlanTask, limit: int | None) -> int:
        final_limit = limit or self.max_results
        if isinstance(task.inputs, dict):
            candidate = task.inputs.get("limit")
            if isinstance(candidate, int) and candidate > 0:
                final_limit = min(final_limit, candidate)
        return max(1, final_limit)

    def _parse_response(
        self,
        response: MCPToolResponse,
        limit: int,
        arguments: Dict[str, Any],
    ) -> List[EvidenceItem]:
        payload = response.best_effort_payload()
        rows: List[Dict[str, Any]] = []
        if isinstance(payload, dict):
            items = payload.get("results") or payload.get("items") or payload.get("rows")
            if isinstance(items, list):
                rows = [entry for entry in items if isinstance(entry, dict)]
        elif isinstance(payload, list):
            rows = [entry for entry in payload if isinstance(entry, dict)]
        items = [
            self._row_to_evidence(index, row, arguments)
            for index, row in enumerate(rows[:limit])
        ]
        return deduplicate_items(items)

    def _row_to_evidence(
        self,
        index: int,
        row: Dict[str, Any],
        arguments: Dict[str, Any],
    ) -> EvidenceItem:
        snippet = build_snippet(str(row.get("snippet") or row.get("content") or ""), self.snippet_chars)
        source = row.get("url") or row.get("source") or row.get("domain") or "web"
        title = row.get("title") or ""
        metadata: Dict[str, str] = {}
        for key in ("title", "snippet", "content"):
            row.pop(key, None)
        for key, value in row.items():
            if value is None:
                continue
            if isinstance(value, (list, dict)):
                continue
            metadata[key] = str(value)
        if title:
            metadata.setdefault("title", title)
        metadata.setdefault("retrieval_strategy", "web-mcp")
        profile = arguments.get("profile") or arguments.get("mode")
        if profile:
            metadata.setdefault("search_profile", str(profile))
        if arguments.get("group"):
            metadata.setdefault("engine_group", str(arguments["group"]))
        if arguments.get("engines"):
            engines_value = arguments["engines"]
            if isinstance(engines_value, list):
                metadata.setdefault("engines", ",".join(str(v) for v in engines_value))
            else:
                metadata.setdefault("engines", str(engines_value))
        return EvidenceItem(
            id=f"web-{index + 1}",
            source=str(source),
            content=snippet,
            score=float(row.get("score", 0.15) or 0.15),
            metadata=metadata,
        )

    def _stub_response(self, query: str, limit: int) -> List[EvidenceItem]:
        snippet = build_snippet(
            "SearxNG integration inactive; returning placeholder web search summary.",
            self.snippet_chars,
        )
        item = EvidenceItem(
            id="web-1",
            source="web-stub",
            content=f"{snippet} query={query}",
            score=0.1,
            metadata={"retrieval_strategy": "web-stub"},
        )
        return [item][:limit]

    def _default_tool(self, config: MCPServerConfig) -> str:
        if config.tools:
            return config.tools[0]
        LOGGER.warning("Web MCP config missing tools list; defaulting to web_search")
        return "web_search"

    def _build_arguments(
        self, task: PlanTask, query: str, limit: int, timeout: int
    ) -> Dict[str, Any]:
        final_limit = max(1, min(limit, self.max_results))
        arguments: Dict[str, Any] = {
            "query": query,
            "limit": final_limit,
            "timeout_seconds": timeout,
        }
        profile_name: str | None = None
        if isinstance(task.inputs, dict):
            raw_profile = task.inputs.get("profile") or task.inputs.get("mode")
            if isinstance(raw_profile, str):
                profile_name = raw_profile.strip().lower() or None
        if profile_name and profile_name in self.profiles:
            profile_arguments = self.profiles[profile_name]
            for key, value in profile_arguments.items():
                arguments.setdefault(key, value)
            arguments.setdefault("profile", profile_name)
        if isinstance(task.inputs, dict):
            for key in (
                "group",
                "engines",
                "categories",
                "language",
                "time_range",
                "site",
                "format",
                "fetch_content",
                "use_text",
            ):
                if key in task.inputs and task.inputs[key] not in (None, ""):
                    arguments[key] = task.inputs[key]
        return arguments


__all__ = ["WebTool"]
