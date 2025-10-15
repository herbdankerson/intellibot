"""Postgres connection helpers for ETL tasks."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from psycopg import Connection

from src.my_agentic_chatbot.storage.connection import psycopg_connection


@contextmanager
def get_connection(**kwargs) -> Iterator[Connection]:
    """Yield a psycopg connection sourced from the shared connection module."""

    with psycopg_connection(**kwargs) as conn:
        yield conn
