"""Tests for the planner component."""

from src.my_agentic_chatbot.planner.main_planner import plan_from_message
from src.my_agentic_chatbot.workflows import policies


def test_plan_contains_budgeted_db_task() -> None:
    plan = plan_from_message("Summarize the workflow approvals policy")
    assert plan.tasks, "Planner should emit at least one task"
    first_task = plan.tasks[0]
    assert first_task.tool == "db_search"
    assert first_task.budget_tokens == policies.DEFAULT_DB_BUDGET_TOKENS
    assert first_task.timeout_seconds == policies.DEFAULT_DB_TIMEOUT_SECONDS


def test_plan_optionally_includes_graph_task() -> None:
    plan = plan_from_message("Explain relationships between agents in the workflow graph")
    tools = {task.tool for task in plan.tasks}
    assert "graph_search" in tools
