"""Tests for the FastMCP client wrapper."""

from __future__ import annotations

from typing import Any

import pytest

from src.my_agentic_chatbot.mcp_client.mcp_client import MCPClient


class DummyResult:
    def __init__(self) -> None:
        self.data = None
        self.structured_content = {"rows": [{"content": "text", "source": "doc"}]}
        self.content: list[str] = []
        self.is_error = False


class DummyClient:
    def __init__(self, transport, name: str, timeout: float) -> None:
        self.transport = transport
        self.name = name
        self.timeout = timeout

    async def __aenter__(self) -> "DummyClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout: float | None = None):
        assert name == "db_search"
        assert arguments == {"query": "test"}
        return DummyResult()


class DummyTransport:
    def __init__(self, url: str, headers: dict[str, str] | None = None) -> None:
        self.url = url
        self.headers = headers or {}


def test_mcp_client_returns_structured_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_transport(url: str, headers: dict[str, str] | None = None):
        captured["headers"] = headers or {}
        return DummyTransport(url=url, headers=headers)

    monkeypatch.setattr(
        "src.my_agentic_chatbot.mcp_client.mcp_client.SSETransport", fake_transport
    )
    monkeypatch.setattr(
        "src.my_agentic_chatbot.mcp_client.mcp_client.FastMCPClient", DummyClient
    )

    client = MCPClient("http://example.com", token="abc", server_name="postgres")
    result = client.call_tool_sync("db_search", {"query": "test"})

    assert result.structured == {"rows": [{"content": "text", "source": "doc"}]}
    assert not result.is_error
    assert captured["headers"]["Authorization"] == "Bearer abc"
