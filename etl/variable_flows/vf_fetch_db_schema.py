"""Variable flow for fetching table metadata from ParadeDB."""

from __future__ import annotations

from typing import List, Tuple

from prefect import task

from etl.tasks.schema_tasks import fetch_table_columns


@task
def fetch_db_schema(table_name: str, schema: str = "public") -> List[Tuple[str, str]]:
    """Return (column, data_type) tuples for the requested table."""
    return fetch_table_columns(table_name, schema)
