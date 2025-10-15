"""Thin wrapper around the stub database tool to satisfy orchestrator imports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from ..schemas import PlanTask, Requirement
from .db_tools import DatabaseTool
from .types import ToolOutcome


@dataclass
class GraphTool(DatabaseTool):
    """Delegates to the stub database tool so workflows can proceed."""

    def execute(
        self,
        task: PlanTask,
        requirement: Requirement,
        *,
        scope: Optional[Iterable[str]] = None,
        overrides: Optional[dict] = None,
    ) -> ToolOutcome:
        return super().execute(task, requirement, scope=scope, overrides=overrides)


__all__ = ["GraphTool"]
