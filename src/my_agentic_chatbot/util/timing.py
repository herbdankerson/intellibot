"""Timing helpers for the workflow orchestrator."""

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Callable, Iterator


def utc_now() -> datetime:
    """Return the current UTC timestamp."""

    return datetime.now(timezone.utc)


@contextmanager
def timer() -> Iterator[Callable[[], float]]:
    """Context manager that yields a callable returning the elapsed time."""

    start = time.perf_counter()
    try:
        yield lambda: time.perf_counter() - start
    finally:
        pass


def remaining_budget(started_at: datetime, timeout_seconds: int) -> float:
    """Return how many seconds of budget remain from a timeout."""

    elapsed = (utc_now() - started_at).total_seconds()
    return max(0.0, timeout_seconds - elapsed)


__all__ = ["remaining_budget", "timer", "utc_now"]
