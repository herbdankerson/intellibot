"""
Row validation tasks.

Perform lightweight validation to ensure parsed rows align with the target
schema before attempting database writes. Unknown columns are rejected to
avoid accidental schema drift.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Tuple


def _normalise_row(row: Dict[str, str], allowed: Iterable[str]) -> Dict[str, str]:
    allowed_set = set(allowed)
    return {key: value for key, value in row.items() if key in allowed_set}


def validate_rows(
    rows: List[Dict[str, str]],
    table_columns: List[str],
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """Split rows into valid and rejected collections.

    A row is considered valid when all of its keys exist in ``table_columns``
    and at least one value survives after filtering. Rejected rows are annotated
    with a ``_reason`` entry to aid debugging.
    """
    allowed = set(table_columns)
    valid: List[Dict[str, str]] = []
    rejected: List[Dict[str, str]] = []

    for row in rows:
        unknown = set(row) - allowed
        cleaned = _normalise_row(row, allowed)
        if unknown:
            rejected_row = dict(row)
            rejected_row["_reason"] = f"unknown columns: {', '.join(sorted(unknown))}"
            rejected.append(rejected_row)
            continue
        if not cleaned:
            rejected_row = dict(row)
            rejected_row["_reason"] = "no recognised columns"
            rejected.append(rejected_row)
            continue
        valid.append(cleaned)

    return valid, rejected
