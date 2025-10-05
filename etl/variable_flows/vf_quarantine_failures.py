"""
Variable flow for quarantining failed files when ingestion fails.

If any files cannot be processed due to parsing or validation errors they
should be moved into a quarantine directory so that you can inspect them
without blocking subsequent runs. This stub logs the files but does not
actually move them.
"""

from __future__ import annotations

import logging
from typing import List

from prefect import task, get_run_logger
from prefect.exceptions import MissingContextError


def _logger():
    try:
        return get_run_logger()
    except MissingContextError:
        return logging.getLogger(__name__)


@task
def quarantine_failures(files: List[str], failed_dir: str) -> None:
    """Move failed files into a quarantine directory."""
    logger = _logger()
    logger.info("Would move %d failed file(s) into %s.", len(files), failed_dir)
    # TODO: implement file moving logic
