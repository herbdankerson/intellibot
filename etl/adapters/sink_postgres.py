"""Postgres connection helpers for ETL tasks."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg

DEFAULT_DSN = "postgresql://agent:agentpass@localhost:5432/agentdb"


def _resolve_dsn() -> str:
    """Return the database connection string.

    Prefers ``PARADE_DB_DSN`` or ``DATABASE_URL`` environment variables and
    falls back to the ParadeDB defaults from ``docker-compose.yml``.
    """
    return (
        os.getenv("PARADE_DB_DSN")
        or os.getenv("DATABASE_URL")
        or DEFAULT_DSN
    )


@contextmanager
def get_connection(**kwargs) -> Iterator[psycopg.Connection]:
    """Yield a psycopg connection and ensure it is closed afterwards."""
    conn = psycopg.connect(_resolve_dsn(), **kwargs)
    try:
        yield conn
    finally:
        conn.close()
