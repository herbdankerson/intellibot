"""Tests for the planner component."""

import json

from src.my_agentic_chatbot.planner.main_planner import plan_from_message
from src.my_agentic_chatbot.constants import (
    DEFAULT_CUSTOM_AGENT_BUDGET_TOKENS,
    DEFAULT_CUSTOM_AGENT_TIMEOUT_SECONDS,
)
from src.my_agentic_chatbot.workflows import policies


class StubPlannerClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.messages = []

    def chat(self, messages):
        self.messages.append(messages)
        return json.dumps(self.payload)

    def close(self) -> None:  # pragma: no cover - compatibility
        pass


def test_plan_contains_budgeted_db_task() -> None:
    payload = {
        "goals": ["Summarize"],
        "tasks": [
            {
                "id": "db-1",
                "description": "Run a focused database search",
                "tool": "db_search",
            }
        ],
    }
    plan = plan_from_message(
        "Summarize the workflow approvals policy",
        client=StubPlannerClient(payload),
    )
    assert plan.tasks, "Planner should emit at least one task"
    first_task = plan.tasks[0]
    assert first_task.tool == "db_search"
    assert first_task.budget_tokens == policies.DEFAULT_DB_BUDGET_TOKENS
    assert first_task.timeout_seconds == policies.DEFAULT_DB_TIMEOUT_SECONDS
    assert first_task.inputs.get("query")
    assert first_task.depends_on == []


def test_plan_optionally_includes_graph_task() -> None:
    payload = {
        "goals": ["Explain relationships"],
        "tasks": [
            {
                "id": "db-1",
                "description": "Database lookup",
                "tool": "db_search",
            },
            {
                "id": "graph-2",
                "description": "Check relationships",
                "tool": "graph_search",
                "requires_approval": True,
            },
        ],
    }
    plan = plan_from_message(
        "Explain relationships between agents in the workflow graph",
        client=StubPlannerClient(payload),
    )
    tools = {task.tool for task in plan.tasks}
    assert "graph_search" in tools
    graph_task = next(task for task in plan.tasks if task.tool == "graph_search")
    assert graph_task.requires_approval is True


def test_plan_raises_on_empty_message() -> None:
    try:
        plan_from_message("   ")
    except ValueError as exc:
        assert "non-empty" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("Planner should reject empty prompts")


def test_plan_defaults_custom_agent_budget() -> None:
    payload = {
        "goals": ["Delegate to custom agent"],
        "tasks": [
            {
                "id": "agent-1",
                "description": "Run custom reasoning",
                "tool": "agent-cheap-worker",
            }
        ],
    }
    plan = plan_from_message(
        "Run the custom agent for extra reasoning",
        client=StubPlannerClient(payload),
    )
    assert plan.tasks[0].budget_tokens == DEFAULT_CUSTOM_AGENT_BUDGET_TOKENS
    assert plan.tasks[0].timeout_seconds == DEFAULT_CUSTOM_AGENT_TIMEOUT_SECONDS
    assert plan.tasks[0].requires_approval is False


def test_plan_normalizes_numeric_identifiers() -> None:
    payload = {
        "goals": ["Normalize"],
        "tasks": [
            {
                "id": 1,
                "description": "Database lookup",
                "tool": "db_search",
                "depends_on": [2],
                "inputs": {"limit": "3"},
            }
        ],
    }
    plan = plan_from_message("Normalize task identifiers", client=StubPlannerClient(payload))
    assert plan.tasks[0].id == "1"
    assert plan.tasks[0].depends_on == ["2"]
