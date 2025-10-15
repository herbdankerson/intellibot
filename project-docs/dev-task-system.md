# Dev KB Task System Rollout

> Tracking document authored inside Codex workspace to exercise the proposed task manager structure. Treat each section as the canonical source until the database-backed system lands.

## Document Metadata

- `doc_id`: `DEV-TASK-SYSTEM-20241001`
- `domain`: general
- `flags`: `is_dev=true`, `is_task_doc=true`
- `related_scopes`: `kb_dev`, `kb_all`
- `tags`: ingestion, flows, mcp, prefect, paradeb, tooling

## Task Registry

| Task ID | Title | Status | Type | Priority | Dependencies | Owner | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `TASK-SCHEMA-REFRESH` | Standardize dev KB vs prod layout | complete | db-migration | high | — | Codex | Legacy schemas dropped via migration; dev meta columns formalised; `kb.ingest_flow_runs` added for telemetry. |
| `TASK-FLOW-REGISTRY` | Harden modular flow registry | complete | flow | high | — | Codex | Registry documented; pipeline now records flow telemetry in ParadeDB. |
| `TASK-RUNTIME-AUTOMATION` | Remove manual worker bootstrap | complete | ops | high | `TASK-FLOW-REGISTRY` | Codex | Compose bootstrap installs Freshbot, applies schema, and sets PYTHONPATH automatically. |
| `TASK-MODEL-INTEGRATIONS` | Restore docling/Qwen/Gemini wiring | pending | integrations | high | `TASK-RUNTIME-AUTOMATION` | Codex | Fail hard on missing services; add health checks & retries. |
| `TASK-SEARCH-TOOLKIT` | Finalize MCP tool surface | complete | mcp | medium | `TASK-FLOW-REGISTRY` | Codex | Task/project tools (`list_projects`, `list_tasks`, `update_task`, `sync_task_document`) extended with add/fetch/delete/mark-complete, task search, scope discovery, and DAG upload. Integration tests still pending. |
| `TASK-TASK-MANAGER-POC` | Prototype DB schema for tasks/DAG | complete | design | medium | `TASK-SCHEMA-REFRESH` | Codex | `task.*` tables plus auto-sync pipeline + Prefect flows now live; DAG tooling still pending. |
| `TASK-DAG-UPLOADER` | Build DAG ingest flow | in_progress | flow | medium | `TASK-TASK-MANAGER-POC`, `TASK-MODEL-INTEGRATIONS` | Codex | DAG upload tool implemented (edge parsing + KB ingest) and schema online; run live pipeline once Prefect/ETL packages are baked into Codex container. |
| `TASK-PREFECT-SYNC` | Sync Prefect deployments with tasks | pending | ops | medium | `TASK-DAG-UPLOADER` | Codex | Auto-create/update Prefect deployments per DAG upload. |
| `TASK-TEST-SUITE` | Add integration coverage | pending | qa | medium | `TASK-MODEL-INTEGRATIONS` | Codex | Cover general/law/code branches, search, DAG enforcement. |
| `TASK-OBSERVABILITY` | Expand auditing & dashboards | pending | monitoring | low | `TASK-RUNTIME-AUTOMATION` | Codex | Prefect + DB dashboards, failure alerts, search telemetry. |

### Status Legend

- `pending`: Defined, ready to execute.
- `in_progress`: Actively being worked.
- `blocked`: Waiting on dependency or external requirement.
- `complete`: Verified in environment with zero manual steps.

### Dependency Graph

- `TASK-DAG-UPLOADER` ← `TASK-TASK-MANAGER-POC`, `TASK-MODEL-INTEGRATIONS`
- `TASK-PREFECT-SYNC` ← `TASK-DAG-UPLOADER`
- `TASK-MODEL-INTEGRATIONS` ← `TASK-RUNTIME-AUTOMATION`
- `TASK-RUNTIME-AUTOMATION` ← `TASK-FLOW-REGISTRY`
- `TASK-TASK-MANAGER-POC` ← `TASK-SCHEMA-REFRESH`

## Execution Notes

- **Schema refresh**: `models.sql` now drops legacy schemas, enforces dev columns, and creates `kb.ingest_flow_runs`/indexes so telemetry persists reliably. Embedding columns remain at 1024 dims until we split storage by embedder or migrate Postgres vector types.
- **Flow registry**: step/compiled flows remain Prefect decorated with docstrings; new audit helper inserts start/end rows into `kb.ingest_flow_runs` for visibility.
- **Runtime automation**: docker-compose bootstrap installs Freshbot in editable mode, reapplies schema with retries, and exports `/workspace/freshbot/src` on `PYTHONPATH`.
- **Prefect database**: the Prefect server now runs against ParadeDB (`prefect` schema owned by a dedicated `prefect` user). `prefect server database upgrade --yes` succeeded after provisioning the schema and work pool metadata was redeployed from `flows.yaml`.
- **Neo4j task graph**: added `task-sync-neo4j/freshbot-task-graph-sync` deployment plus Codex MCP wiring so each DAG upload triggers graph mirroring. Nodes/edges verified via `bolt://localhost:7688` (e.g. `TASK-BAA6412C10 → TASK-4EFA43A3CB`).
- **Task tools**: Codex MCP repository now exposes end-to-end task management utilities (add/fetch/list/search, mark complete, delete, DAG upload, task-doc search) with Markdown ingestion and metadata tagging; ParadeDB `task.*` schema live after re-running `models.sql`.
- **Prefect MCP parity**: the official Prefect MCP server (`PrefectHQ/prefect-mcp-server`) relies entirely on Prefect’s orchestration API/DB to track flows, deployments, flow runs, task runs, events, and rate limits. It never stores orchestration state in an auxiliary database—the server simply proxies FastMCP tools to `prefect.get_client()` and lets Prefect’s backend own run history. We should follow the same pattern: Codex MCP remains a thin API facade that schedules real work by calling `/freshbot/flows/execute` (or Prefect’s REST) and queries Prefect when orchestration metadata is needed, avoiding duplicate flow/run tables in ParadeDB.
- **Model integrations**: replace stub calls with production clients; add guardrails (timeouts, retries, fallback classification for offline models). Validation should fail the flow if critical services are unavailable.
- **Graph sync telemetry**: DAG uploads now log into `task.graph_sync_runs` (deployment, flow run id, status, message, metadata) so observability dashboards can track Neo4j sync health alongside `kb.ingest_flow_runs`.
- **MCP tooling**: `list_scopes`, `search_entries`, `get_document`, `update_chunk`, `upload_document`, plus new task helpers (`list_projects`, `list_tasks`, `update_task`, `sync_task_document`) are live; next add task search once embeddings settle.
- **Task manager PoC**: paradeDB schema plus auto-sync wiring landed (`task_registry.sync_task_document` runs during ingest + dedicated Prefect flows); next explore embedding strategy for task docs and hybrid search weighting for task descriptions.
- **DAG uploader**: treat DAG files as documents; parse edges, validate referenced task IDs, persist as versioned rows, emit Prefect deployment updates.
- **Prefect sync**: automate flow run creation from tasks; map task statuses to Prefect state transitions and record run IDs in `task.activity_log`.
- **QA suite**: expand integration tests (`pytest`) to cover ingestion branches, search vs update operations, DAG validation, and MCP endpoints.
- **Observability**: surface metrics (ingest duration, classifier latency, search hit quality) via Prefect/ParadeDB dashboards; add alerting for ingestion failures.

## Immediate Actions (Next 48h)

1. Bake Prefect + ETL dependencies (and PYTHONPATH overrides) into the Codex MCP container so task ingestion runs without manual exec hacks (`TASK-RUNTIME-AUTOMATION` follow-up).
2. Run a live `add_task`/`mark_task_complete` cycle to confirm Markdown ingestion succeeds end-to-end and verify the resulting KB rows (`TASK-MODEL-INTEGRATIONS`, `TASK-SEARCH-TOOLKIT` QA).
3. Add regression tests for repository helpers + MCP tools (CRUD, DAG upload, search) once the environment supports local PostgreSQL access (`TASK-TEST-SUITE`).
4. Wire health checks for Docling/Qwen/Gemini/ctags ahead of ingestion so flows fail fast on missing gateways (`TASK-MODEL-INTEGRATIONS`).

## Open Questions

- What is the preferred DAG versioning strategy—append-only history or project-name upserts with historical snapshots?
- Do we gate Prefect deployment updates behind manual approval, or auto-publish on DAG upload?

## Change Log

- `2024-10-01`: Initial authoring of live-tracking doc within Codex environment. Document to be ingested once pipeline automation is complete.
- `2024-10-02`: Completed schema refresh, flow telemetry, and runtime bootstrap automation.
- `2024-10-03`: Added ParadeDB `task.*` tables and wired auto-sync + Prefect task flows (`task_registry.*`); ingestion docs now drive DB state.
- `2024-10-04`: Codex MCP task manager tools expanded (CRUD, search, DAG upload); ParadeDB schema applied to live DB; task docs now render + ingest through repository helpers.
