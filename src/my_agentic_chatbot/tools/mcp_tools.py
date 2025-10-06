"""Generic MCP tool adapters for specialized servers."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

from ..config import MCPServerConfig, get_settings
from ..mcp_client.mcp_client import MCPClient, MCPToolResponse
from ..schemas import EvidenceItem, PlanTask
from ..util.text import build_snippet

LOGGER = logging.getLogger(__name__)


def _normalize_arguments(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    LOGGER.debug("Unexpected arguments payload for MCP tool", extra={"payload": raw})
    return {}


def _first_list_entry(payload: Any) -> Iterable[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        rows = payload.get("items") or payload.get("rows") or payload.get("results")
        if isinstance(rows, list):
            return rows
    return [payload]


@dataclass
class MCPJsonTool:
    """Adapter that executes JSON-friendly MCP tools and yields evidence snippets."""

    server_name: str
    default_tool: str | None = None
    id_prefix: str = "mcp"
    max_results: int = 4
    snippet_chars: int = 320
    client: MCPClient | None = None
    configured_tool: str | None = None

    def __post_init__(self) -> None:
        if self.client is not None:
            return
        try:
            config = get_settings().mcp_server(self.server_name)
        except KeyError:
            LOGGER.warning(
                "MCP server configuration missing",
                extra={"server": self.server_name},
            )
            return
        tool_name = self.default_tool or self._default_tool(config)
        self.configured_tool = tool_name
        self.client = MCPClient(
            base_url=config.url,
            token=config.token,
            server_name=f"{config.name}-client",
        )

    def execute(self, task: PlanTask) -> List[EvidenceItem]:
        tool_name = self._resolve_tool_name(task)
        arguments = _normalize_arguments(task.inputs.get("arguments") if isinstance(task.inputs, dict) else None)
        if not arguments and isinstance(task.inputs, dict):
            for candidate_key in ("query", "cypher", "payload"):
                candidate = task.inputs.get(candidate_key)
                if candidate is not None:
                    arguments.setdefault(candidate_key, candidate)
        if not arguments and task.description:
            arguments.setdefault("query", task.description)
        if self.client is None or tool_name is None:
            return self._stub_response(tool_name, arguments)
        try:
            response = self.client.call_tool_sync(tool_name, arguments)
        except Exception as exc:  # pragma: no cover - defensive guard
            LOGGER.exception(
                "MCP tool call failed",
                extra={"tool": tool_name, "server": self.server_name},
            )
            return self._stub_response(tool_name, arguments, error=str(exc))
        return self._build_evidence(response)

    def _resolve_tool_name(self, task: PlanTask) -> str | None:
        if not isinstance(task.inputs, dict):
            return self.configured_tool
        candidate = task.inputs.get("tool") or task.inputs.get("operation")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        return self.configured_tool

    def _default_tool(self, config: MCPServerConfig) -> str | None:
        if config.tools:
            return config.tools[0]
        LOGGER.debug("No tool list for MCP server", extra={"server": config.name})
        return None

    def _build_evidence(self, response: MCPToolResponse) -> List[EvidenceItem]:
        payload = response.best_effort_payload()
        items = list(_first_list_entry(payload))
        evidence: List[EvidenceItem] = []
        for index, entry in enumerate(items[: self.max_results]):
            if isinstance(entry, EvidenceItem):  # pragma: no cover - defensive
                evidence.append(entry)
                continue
            if isinstance(entry, dict):
                content = json.dumps(entry, ensure_ascii=False, indent=2)
                source = entry.get("source") or entry.get("id") or self.server_name
            else:
                content = str(entry)
                source = self.server_name
            snippet = build_snippet(content, self.snippet_chars)
            evidence.append(
                EvidenceItem(
                    id=f"{self.id_prefix}-{index + 1}",
                    source=str(source),
                    content=snippet,
                    score=float(getattr(entry, "score", 0.5) or 0.5),
                    metadata={
                        "mcp_server": self.server_name,
                        "tool": self._resolve_id_prefix(),
                    },
                )
            )
        if evidence:
            return evidence
        fallback = build_snippet(json.dumps(payload, ensure_ascii=False), self.snippet_chars)
        return [
            EvidenceItem(
                id=f"{self.id_prefix}-1",
                source=self.server_name,
                content=fallback,
                score=0.1,
                metadata={"mcp_server": self.server_name, "tool": self._resolve_id_prefix()},
            )
        ]

    def _resolve_id_prefix(self) -> str:
        return self.configured_tool or self.default_tool or self.server_name

    def _stub_response(
        self,
        tool_name: str | None,
        arguments: Dict[str, Any],
        *,
        error: str | None = None,
    ) -> List[EvidenceItem]:
        LOGGER.warning(
            "MCP tool unavailable; returning no evidence",
            extra={
                "server": self.server_name,
                "tool": tool_name or self.default_tool,
                "error": error,
            },
        )
        return []


@dataclass
class SequentialThinkingTool(MCPJsonTool):
    """Specialised adapter for the Sequential Thinking MCP server."""

    def execute(self, task: PlanTask) -> List[EvidenceItem]:
        arguments = self._prepare_arguments(task)
        if self.client is None and self.configured_tool is None:
            return self._stub_response(self.configured_tool, arguments)
        tool_name = self._resolve_tool_name(task) or self.configured_tool or "sequentialthinking"
        try:
            response = self.client.call_tool_sync(tool_name, arguments) if self.client else None
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.exception(
                "Sequential thinking MCP call failed",
                extra={"tool": tool_name, "server": self.server_name},
            )
            return self._stub_response(tool_name, arguments, error=str(exc))
        if response is None:
            return self._stub_response(tool_name, arguments)
        items = self._build_evidence(response)
        payload = response.best_effort_payload()
        arguments_json = json.dumps(arguments, ensure_ascii=False)
        payload_json = json.dumps(payload, ensure_ascii=False)
        for item in items:
            item.metadata.setdefault("mcp_server", self.server_name)
            item.metadata.setdefault("tool", tool_name)
            item.metadata["thought_arguments"] = arguments_json
            item.metadata["thought_payload"] = payload_json
        return items

    def _prepare_arguments(self, task: PlanTask) -> Dict[str, Any]:
        base: Dict[str, Any] = {
            "thought": task.inputs.get("thought") if isinstance(task.inputs, dict) else None,
            "nextThoughtNeeded": True,
            "thoughtNumber": 1,
            "totalThoughts": 3,
        }
        if isinstance(task.inputs, dict):
            base.update({k: v for k, v in task.inputs.items() if k not in {"tool", "operation"}})
        if not base.get("thought"):
            base["thought"] = task.description
        if isinstance(base.get("thoughtNumber"), str) and str(base["thoughtNumber"]).isdigit():
            base["thoughtNumber"] = int(base["thoughtNumber"])
        if isinstance(base.get("totalThoughts"), str) and str(base["totalThoughts"]).isdigit():
            base["totalThoughts"] = int(base["totalThoughts"])
        base["nextThoughtNeeded"] = bool(base.get("nextThoughtNeeded", True))
        return base
