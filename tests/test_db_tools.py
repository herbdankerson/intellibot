"""Tests for the database tool adapter."""

from src.my_agentic_chatbot.tools.db_tools import DatabaseTool


def test_database_tool_enforces_limits() -> None:
    tool = DatabaseTool(max_results=2, snippet_chars=120)
    results = tool.search("agentic retrieval")
    assert len(results) <= 2
    for item in results:
        assert len(item.content) <= 120
        assert "embedding" not in item.metadata
