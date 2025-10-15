"""Stub MCP tool adapters for temporary end-to-end validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from ..schemas import PlanTask, Requirement
from .db_tools import DatabaseTool


@dataclass
class MCPJsonTool(DatabaseTool):
    """Shares the stub evidence used by the database tool."""

    server_name: str = "stub-mcp"
    default_tool: Optional[str] = None
    id_prefix: Optional[str] = None

    def execute(
        self,
        task: PlanTask,
        requirement: Requirement,
        **_: Any,
    ):
        outcome = super().execute(task, requirement)
        return outcome.evidence


@dataclass
class SequentialThinkingTool(DatabaseTool):
    """Feeds canned notes to unblock planner supervision."""

    server_name: Optional[str] = None
    default_tool: Optional[str] = None
    id_prefix: Optional[str] = None

    def execute(
        self,
        task: PlanTask,
        requirement: Requirement,
        *,
        scope: Optional[Iterable[str]] = None,
        overrides: Optional[dict] = None,
    ):
        outcome = super().execute(task, requirement, scope=scope, overrides=overrides)
        for note in (
            "sequential thinking stub used",
            "real MCP server disabled for this smoke run",
        ):
            if note not in outcome.notes:
                outcome.notes.append(note)
        return outcome.evidence


__all__ = ["MCPJsonTool", "SequentialThinkingTool"]
