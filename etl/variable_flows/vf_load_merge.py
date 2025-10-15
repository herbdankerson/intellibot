"""
Variable flow for merging staged data into the final table.

For the ParadeDB smoke test the rows are written directly into the target
inside ``vf_stage_write`` so this task currently logs the intended behaviour.
Retain the structure so it can be replaced with a true merge/upsert later.
"""

from __future__ import annotations

import logging
from typing import Optional

from prefect import task, get_run_logger
from prefect.exceptions import MissingContextError


def _logger():
    try:
        return get_run_logger()
    except MissingContextError:
        return logging.getLogger(__name__)


@task
def load_merge(
    table_name: str,
    schema: str = "public",
    mode: str = "append",
    upsert_key: Optional[str] = None,
) -> None:
    """Placeholder merge task."""
    logger = _logger()
    if mode == "upsert":
        logger.info(
            "Skipping merge placeholder — rows already inserted into %s.%s (would use key %s).",
            schema,
            table_name,
            upsert_key,
        )
    else:
        logger.info(
            "Skipping merge placeholder — rows already appended into %s.%s.",
            schema,
            table_name,
        )
