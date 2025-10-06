"""Tests for generic MCP tool adapters."""

from src.my_agentic_chatbot.schemas import PlanTask
from src.my_agentic_chatbot.tools.mcp_tools import MCPJsonTool, SequentialThinkingTool


def _make_task(tool: str, description: str, inputs: dict | None = None) -> PlanTask:
    return PlanTask(
        id="task-1",
        description=description,
        tool=tool,
        budget_tokens=100,
        timeout_seconds=5,
        requires_approval=False,
        inputs=inputs or {},
        depends_on=[],
    )


def test_mcp_json_tool_stub_when_config_missing() -> None:
    tool = MCPJsonTool(server_name="missing-server", id_prefix="test")
    task = _make_task("neo4j_cypher", "List nodes", {"arguments": {"query": "MATCH (n) RETURN n"}})
    evidence = tool.execute(task)
    assert evidence == []


def test_sequential_thinking_tool_builds_defaults() -> None:
    tool = SequentialThinkingTool(server_name="missing-server", id_prefix="seq")
    task = _make_task("agent-sequentialthinking", "Consider next steps", {})
    evidence = tool.execute(task)
    assert evidence == []
