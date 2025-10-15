"""
High-level Prefect flow for ingesting data into a database table.

This module defines ``flow_table_ingest`` which orchestrates the ingestion of
CSV files into ParadeDB. It demonstrates end-to-end connectivity by parsing
files, validating the rows against live table metadata and inserting them into
Postgres. The implementation intentionally remains lightweight so that future
work can expand validation, staging and merge semantics without reworking the
flow structure.
"""

from __future__ import annotations

from typing import List, Optional

from prefect import flow, get_run_logger

from etl.variable_flows.vf_discover_files import discover_files
from etl.variable_flows.vf_parse_batch import parse_files
from etl.variable_flows.vf_apply_transforms import apply_transforms
from etl.variable_flows.vf_fetch_db_schema import fetch_db_schema
from etl.variable_flows.vf_validate_rows import validate_rows_task
from etl.variable_flows.vf_stage_write import stage_write
from etl.variable_flows.vf_load_merge import load_merge


@flow
def flow_table_ingest(
    table_name: str,
    source_path: str,
    file_glob: str = "**/*",
    mode: str = "append",
    upsert_key: Optional[str] = None,
    transforms: Optional[List[str]] = None,
    schema: str = "public",
) -> dict:
    """Orchestrate ingestion of data files into a ParadeDB table."""
    logger = get_run_logger()
    logger.info(
        "Starting ingestion for table %s from %s (glob: %s, mode: %s)",
        table_name,
        source_path,
        file_glob,
        mode,
    )

    files = discover_files(source_path, file_glob)
    file_count = len(files)
    logger.info("Discovered %d file(s) for ingestion.", file_count)

    if file_count == 0:
        return {
            "table": table_name,
            "files": 0,
            "rows": 0,
            "inserted": 0,
            "rejected": 0,
            "status": "no_files",
        }

    rows = parse_files(files)
    row_count = len(rows)
    logger.info("Parsed a total of %d row(s) across all files.", row_count)

    rows = apply_transforms(rows, transforms)

    column_info = fetch_db_schema(table_name, schema)
    column_names = [name for name, _ in column_info]
    if not column_names:
        raise ValueError(f"No columns found for {schema}.{table_name}; ensure the table exists.")

    valid_rows, rejected_rows = validate_rows_task(rows, column_names)
    valid_count = len(valid_rows)
    rejected_count = len(rejected_rows)
    logger.info("Validation complete: %d valid row(s), %d rejected.", valid_count, rejected_count)

    inserted = stage_write(valid_rows, table_name, schema)
    load_merge(table_name, schema, mode=mode, upsert_key=upsert_key)

    status = "success" if inserted else "no_valid_rows"
    return {
        "table": table_name,
        "files": file_count,
        "rows": row_count,
        "inserted": inserted,
        "rejected": rejected_count,
        "status": status,
    }
