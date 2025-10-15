"""
Variable flow for applying a sequence of named transforms to rows.

Transforms are plain Python callables registered in ``etl.tasks.transform_tasks``.
The flow looks up the function by name, applies it to the input rows and
returns the result. Unknown transform names are silently ignored.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List

from prefect import task, get_run_logger
from prefect.exceptions import MissingContextError

from etl.tasks import transform_tasks


def _logger():
    try:
        return get_run_logger()
    except MissingContextError:
        return logging.getLogger(__name__)


@task
def apply_transforms(rows: List[Dict[str, str]], transform_names: List[str] | None) -> List[Dict[str, str]]:
    """Apply a sequence of transform functions to the input rows."""
    logger = _logger()
    if not transform_names:
        return rows

    transformed = rows
    for name in transform_names:
        func: Callable = getattr(transform_tasks, name, None)
        if func is None:
            logger.warning("Transform %s is not defined; skipping.", name)
            continue
        transformed = func(transformed)
    return transformed
