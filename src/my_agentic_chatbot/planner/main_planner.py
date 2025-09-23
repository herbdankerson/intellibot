"""Planner entry point that produces task plans from user messages."""

from __future__ import annotations

from itertools import count
from typing import List

from ..schemas import Plan, PlanTask
from ..workflows import policies

_TASK_COUNTER = count(1)

_GRAPH_KEYWORDS = {
    "relationship",
    "relationships",
    "graph",
    "path",
    "connect",
    "connected",
    "connection",
}


def _next_task_id(prefix: str) -> str:
    return f"{prefix}-{next(_TASK_COUNTER)}"


def plan_from_message(message: str, *, model: str | None = None) -> Plan:
    """Return a deterministic plan for the provided user message."""

    normalized = message.strip()
    if not normalized:
        raise ValueError("Planner requires a non-empty message")

    goals: List[str] = [normalized]
    tasks: List[PlanTask] = []

    tasks.append(
        PlanTask(
            id=_next_task_id("db"),
            description=f"Run a focused database search for: {normalized}",
            tool="db_search",
            budget_tokens=policies.DEFAULT_DB_BUDGET_TOKENS,
            timeout_seconds=policies.DEFAULT_DB_TIMEOUT_SECONDS,
        )
    )

    if _should_use_graph(normalized):
        tasks.append(
            PlanTask(
                id=_next_task_id("graph"),
                description="Verify multi-hop relationships via the graph tool",
                tool="graph_search",
                budget_tokens=policies.DEFAULT_GRAPH_BUDGET_TOKENS,
                timeout_seconds=policies.DEFAULT_GRAPH_TIMEOUT_SECONDS,
                requires_approval=True,
            )
        )

    acceptance_criteria = [
        "Answer references evidence item identifiers",
        "Unresolved assumptions are captured for follow-up",
    ]
    stop_conditions = ["Acceptance criteria satisfied", "Evidence budget consumed"]

    return Plan(
        goals=goals,
        assumptions=[],
        info_needed=[normalized],
        tasks=tasks,
        stop_conditions=stop_conditions,
        acceptance_criteria=acceptance_criteria,
    )


def _should_use_graph(message: str) -> bool:
    tokens = {token.strip(".,!?:;\"' ").lower() for token in message.split()}
    return any(token in _GRAPH_KEYWORDS for token in tokens)


__all__ = ["plan_from_message"]
