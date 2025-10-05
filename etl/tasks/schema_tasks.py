"""
Database schema inspection tasks.

Utilities for fetching table metadata required for validation and loading
logic. These helpers rely on ``psycopg`` to introspect ParadeDB at runtime.
"""

from __future__ import annotations

from typing import List, Tuple

from psycopg import sql

from etl.adapters import get_connection


def fetch_table_columns(table_name: str, schema: str = "public") -> List[Tuple[str, str]]:
    """Return column names and data types for the target table."""
    query = sql.SQL(
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (schema, table_name))
            return cur.fetchall()
