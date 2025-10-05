"""
Command-line entrypoint for running table-specific ingestion flows.

The launcher reads the table registry defined in ``etl/registry/etl_tables.yml``
and exposes simple commands for listing available tables and triggering
ingestion runs.  It is designed to provide a minimal CLI wrapper around
Prefect flows so that you can execute ingests without having to write any
Python code or understand the underlying internals.

Usage examples::

    # List all eligible tables as defined in the registry
    python -m etl.flows.flow_launcher list-tables

    # Run ingestion for the ``test`` table defined in the registry
    python -m etl.flows.flow_launcher run --table test

When invoked, the run command will look up the table configuration in the
registry, import the appropriate flow function by name and execute it with
parameters provided in the YAML file.  Additional options can be passed at
runtime to override defaults (e.g. ``--file-glob``).
"""

import importlib
from pathlib import Path

import typer

from etl.utils.config import load_registry

app = typer.Typer(help="Launch ETL flows defined in the registry.")


@app.command("list-tables")
def list_tables(registry_path: str = "etl/registry/etl_tables.yml") -> None:
    """List all tables defined in the registry."""
    cfg = load_registry(registry_path)
    tables = cfg.get("tables", {})
    if not tables:
        typer.echo("No tables defined in registry.")
        return
    for name, table_cfg in tables.items():
        src = table_cfg.get("source_dir", "<unspecified>")
        typer.echo(f"{name}\t{src}")


@app.command("run")
def run(
    table: str,
    registry_path: str = "etl/registry/etl_tables.yml",
    source_path: str | None = None,
    file_glob: str | None = None,
    mode: str | None = None,
    upsert_key: str | None = None,
    transforms: str | None = None,
    schema: str | None = None,
) -> None:
    """
    Trigger an ingestion flow for a specific table.

    Args:
        table: Name of the table to ingest.  Must exist in the registry.
        registry_path: Path to the YAML registry file.
        source_path: Optional override for the table's source directory.
        file_glob: Optional override for the table's file glob pattern.
        mode: Optional override for the table's load mode (append/upsert).
        upsert_key: Optional override for the upsert key.
        transforms: Optional comma-separated list of transforms.
        schema: Optional override for the target schema.
    """
    cfg = load_registry(registry_path)
    defaults = cfg.get("defaults", {})
    table_cfg = cfg.get("tables", {}).get(table)
    if not table_cfg:
        raise typer.BadParameter(f"Table {table!r} is not defined in the registry.")

    # Determine runtime values by merging CLI inputs, table-specific overrides and defaults.
    runtime_source = source_path or table_cfg.get("source_dir")
    runtime_glob = file_glob or table_cfg.get("file_glob") or defaults.get("file_glob", "**/*")
    runtime_mode = mode or table_cfg.get("mode") or defaults.get("mode", "append")
    runtime_upsert_key = upsert_key or table_cfg.get("upsert_key")
    runtime_transforms: list[str] | None
    if transforms:
        runtime_transforms = [t.strip() for t in transforms.split(",") if t.strip()]
    else:
        runtime_transforms = table_cfg.get("transforms")
    runtime_schema = schema or defaults.get("schema", "public")

    # Import the flow to run.  We expect the function name to be declared
    # either in the table-specific config or the defaults.  If omitted, default
    # to ``flow_table_ingest``.
    flow_name = table_cfg.get("flow_name") or "flow_table_ingest"
    module = importlib.import_module(f"etl.flows.flow_table_ingest")
    flow_func = getattr(module, flow_name, None)
    if flow_func is None:
        raise ImportError(f"Cannot find flow {flow_name} in etl.flows.flow_table_ingest")

    # Execute the flow synchronously.  In Prefect 2.x this will run the flow
    # immediately; adjust as needed for deployment/execution via server/cloud.
    typer.echo(f"Running {flow_name} for table {table}...")
    result = flow_func(
        table_name=table,
        source_path=runtime_source,
        file_glob=runtime_glob,
        mode=runtime_mode,
        upsert_key=runtime_upsert_key,
        transforms=runtime_transforms,
        schema=runtime_schema,
    )
    # Flow returns a state object in Prefect.  If you want to capture the
    # result synchronously you can call .result() or .state.result() depending
    # on your Prefect version.
    # Here we simply print a confirmation message.
    typer.echo(f"Ingestion flow for {table} triggered: {result}")


if __name__ == "__main__":
    app()
