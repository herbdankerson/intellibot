"""Minimal MCP client wrapper used by the orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class MCPClient:
    """Tiny client that pretends to execute MCP tool calls."""

    base_url: str
    default_headers: Dict[str, str] = field(default_factory=dict)

    def call_tool_sync(self, name: str, arguments: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """Return a mock response payload for the requested tool."""

        payload = {
            "tool": name,
            "arguments": arguments or {},
            "ok": True,
        }
        return payload


__all__ = ["MCPClient"]
