"""Tests for generic MCP tool adapters."""

import pytest

from src.my_agentic_chatbot.tools.mcp_tools import MCPJsonTool, SequentialThinkingTool


def test_mcp_json_tool_errors_when_config_missing() -> None:
    with pytest.raises(RuntimeError, match="missing-server"):
        MCPJsonTool(server_name="missing-server", id_prefix="test")


def test_sequential_thinking_tool_requires_configuration() -> None:
    with pytest.raises(RuntimeError, match="missing-server"):
        SequentialThinkingTool(server_name="missing-server", id_prefix="seq")
