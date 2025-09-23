"""Placeholder Gemini router callback for LiteLLM."""

from __future__ import annotations


def gemini_failover(*args, **kwargs):  # pragma: no cover - integration hook
    """Return without modifying the request.

    The actual failover logic can be implemented here when LiteLLM router
    callbacks are wired into the runtime environment.
    """

    return None


__all__ = ["gemini_failover"]
