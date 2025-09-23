"""Tests for the database tool adapter."""

from src.my_agentic_chatbot.mcp_client.mcp_client import MCPToolResponse
from src.my_agentic_chatbot.tools.db_tools import DatabaseTool


class StubMCPClient:
    def __init__(self, response: MCPToolResponse) -> None:
        self.response = response
        self.calls = []

    def call_tool_sync(self, name: str, arguments):
        self.calls.append((name, arguments))
        return self.response


def test_database_tool_enforces_limits() -> None:
    rows = [
        {
            "snippet": "Agentic retrieval pipelines blend planners and tool calls.",
            "source": "kb:1",
            "score": 2.0,
            "embedding": [0.1, 0.2],
        },
        {
            "content": "Prefect coordinates approvals and retries.",
            "document_id": "doc-2",
            "rank": 1.0,
        },
        {
            "content": "Extra row that should be truncated.",
            "document_id": "doc-3",
        },
    ]
    client = StubMCPClient(MCPToolResponse(data=rows, structured=None, content=[], is_error=False))
    tool = DatabaseTool(max_results=2, snippet_chars=120, client=client, tool_name="db_search")
    results = tool.search("agentic retrieval")
    assert len(results) == 2
    for item in results:
        assert len(item.content) <= 120
        assert "embedding" not in item.metadata
        assert item.metadata.get("retrieval_strategy")


def test_database_tool_fallback_to_content() -> None:
    response = MCPToolResponse(data=None, structured=None, content=["Raw text block"], is_error=False)
    tool = DatabaseTool(max_results=1, snippet_chars=50, client=StubMCPClient(response), tool_name="db_search")
    results = tool.search("fallback")
    assert len(results) == 1
    assert results[0].content.startswith("Raw text block")
