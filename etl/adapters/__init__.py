"""Adapters for sources and sinks."""

from .sink_postgres import get_connection

__all__ = ["get_connection"]
