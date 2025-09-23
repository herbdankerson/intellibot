"""In-memory event logging used for local development."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, List

from ..schemas import ExecutionEvent, ExecutionReport, TaskStatus


def create_event(task_id: str, status: TaskStatus, message: str = "") -> ExecutionEvent:
    """Create an execution event stamped with the current time."""

    return ExecutionEvent(
        task_id=task_id,
        status=status,
        message=message,
        timestamp=datetime.now(timezone.utc),
    )


@dataclass
class EventLogger:
    """Very small accumulator for execution events."""

    events: List[ExecutionEvent] = field(default_factory=list)

    def log(self, event: ExecutionEvent) -> None:
        self.events.append(event)

    def extend(self, events: Iterable[ExecutionEvent]) -> None:
        for event in events:
            self.log(event)

    def export(self) -> List[ExecutionEvent]:
        return list(self.events)

    def attach(self, report: ExecutionReport) -> None:
        for event in self.events:
            report.record(event)


__all__ = ["EventLogger", "create_event"]
