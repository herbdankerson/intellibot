"""
Variable flow for archiving processed files upon successful ingestion.

After a successful run you may wish to move the input files into an archive
directory to avoid reprocessing them. This stub logs the files but does not
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
def archive_success(files: List[str], archive_dir: str) -> None:
    """Archive processed files into a designated directory."""
    logger = _logger()
    logger.info("Would archive %d file(s) into %s.", len(files), archive_dir)
    # TODO: implement file moving logic
