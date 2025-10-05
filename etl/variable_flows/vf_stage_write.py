"""
Variable flow for writing validated rows to the target table.

For the initial ParadeDB integration we write directly into the destination
schema. This keeps the test ETL minimal while still exercising database
connectivity. When you are ready to introduce a true staging layer you can
swap this implementation for one that targets ``__stg_<table>`` first.
"""

from __future__ import annotations

import logging
from typing import Dict, Iterable, List

from prefect import task, get_run_logger
from prefect.exceptions import MissingContextError
from psycopg import sql

from etl.adapters import get_connection


def _logger():
    try:
        return get_run_logger()
    except MissingContextError:
        return logging.getLogger(__name__)


def _ordered_columns(rows: Iterable[Dict[str, str]]) -> List[str]:
    columns: List[str] = []
    for row in rows:
        for key in row.keys():
            if key not in columns:
                columns.append(key)
    return columns


@task
def stage_write(rows: List[Dict[str, str]], table_name: str, schema: str = "public") -> int:
    """Write rows directly into ``schema.table_name``."""
    logger = _logger()
    if not rows:
        logger.info("No rows to write for %s.%s.", schema, table_name)
        return 0

    columns = _ordered_columns(rows)
    insert_stmt = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({})").format(
        sql.Identifier(schema),
        sql.Identifier(table_name),
        sql.SQL(", ").join(sql.Identifier(col) for col in columns),
        sql.SQL(", ").join(sql.Placeholder() for _ in columns),
    )

    with get_connection() as conn:
        with conn.cursor() as cur:
            for row in rows:
                values = [row.get(col) for col in columns]
                cur.execute(insert_stmt, values)
        conn.commit()

    logger.info("Inserted %d row(s) into %s.%s.", len(rows), schema, table_name)
    return len(rows)
