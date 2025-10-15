"""
Metrics and logging tasks.

Centralise metrics reporting and logging in your ETL framework so that it's
easy to add counters, timers and structured logs in a consistent way.  This
stub illustrates where you might build such functionality.
"""

from typing import Dict


def emit_metrics(run_info: Dict[str, int]) -> None:
    """Emit metrics about an ingestion run.

    Args:
        run_info: A dictionary containing counts and statuses.
    """
    # TODO: integrate with your monitoring/observability platform
    print(f"ETL run metrics: {run_info}")
