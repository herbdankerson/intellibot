"""Tracing helpers for correlating workflow runs."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Dict

from .timing import utc_now


@dataclass
class RunContext:
    """Small container describing a workflow run."""

    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: str = field(default_factory=lambda: utc_now().isoformat())
    metadata: Dict[str, str] = field(default_factory=dict)


def generate_run_id() -> str:
    """Return a unique identifier suitable for run correlation."""

    return uuid.uuid4().hex


__all__ = ["RunContext", "generate_run_id"]
