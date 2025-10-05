"""
Variable flow for validating parsed rows against the target table's schema.

This flow delegates to a task in ``etl.tasks.validate_tasks`` to perform
structural validation. It returns two collections: the rows that passed
validation and the rows that were rejected.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Tuple

from prefect import task, get_run_logger
from prefect.exceptions import MissingContextError

from etl.tasks.validate_tasks import validate_rows


def _logger():
    try:
        return get_run_logger()
    except MissingContextError:
        return logging.getLogger(__name__)


@task
def validate_rows_task(rows: List[Dict[str, str]], table_columns: List[str]) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """Validate rows against allowed columns."""
    logger = _logger()
    valid, rejected = validate_rows(rows, table_columns)
    logger.info("Validated %d rows; %d rejected.", len(valid), len(rejected))
    return valid, rejected
