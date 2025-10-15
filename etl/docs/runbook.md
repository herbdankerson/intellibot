# ETL Framework Runbook

This runbook explains how to operate the modular ETL framework.

## Registry

All tables eligible for ingestion are defined in [`etl/registry/etl_tables.yml`](../registry/etl_tables.yml). Each table entry specifies:

- `source_dir`: folder containing files to ingest.
- `archive_dir` and `failed_dir`: where to move processed or problematic files.
- `file_glob`: glob pattern matching eligible files.
- `mode`: either `append` or `upsert`.
- `transforms`: optional list of transform names (see `etl/tasks/transform_tasks.py`).

## Running an ingest

Use the launcher script to list tables and run an ingest:

```
# List available tables
python -m etl.flows.flow_launcher list-tables

# Run ingestion for the test table
python -m etl.flows.flow_launcher run --table test
```

The run command will call the generic `flow_table_ingest` defined in
`etl/flows/flow_table_ingest.py`. The flow will discover files, parse them,
apply transforms and (in a full implementation) write them to your database.

## Extending the framework

As your data model evolves you can add new variable flows in `etl/variable_flows`
and reference them from the orchestrator flow.  When implementing actual
database interaction you may wish to create a `sink_postgres.py` in
`etl/adapters` to encapsulate connection pooling and SQL execution.
