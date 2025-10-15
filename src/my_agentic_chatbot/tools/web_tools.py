"""Stub web tool returning deterministic evidence for end-to-end smoke tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from ..schemas import PlanTask, Requirement
from .db_tools import DatabaseTool
from .types import ToolOutcome


@dataclass
class WebTool(DatabaseTool):
    """Reuses the stub database evidence to emulate web search results."""

    def execute(
        self,
        task: PlanTask,
        requirement: Requirement,
        *,
        scope: Optional[Iterable[str]] = None,
        overrides: Optional[dict] = None,
        limit: Optional[int] = None,
    ) -> ToolOutcome:
        return super().execute(task, requirement, scope=scope, overrides=overrides, limit=limit)


__all__ = ["WebTool"]
