# Freshbot Task Tracker

_Last updated: 2025-10-11 18:33:08Z_

## Current Status
- API stack is live with `PYTHONPATH=/app/src:/app`; `/freshbot/flows/execute` drives Prefect deployments through the API. Qwen chat runs now allow up to 240s for the upstream proxy to respond; latest smoke run (`flow_run_id=888b79f8-d3c2-412f-9b8d-59a3fa3e9d4c`) returned “Prefect offers robust scheduling, real-time monitoring…”.
- Document ingestion flow executed via Prefect (`flow_run_id=4cd3e684-6f0c-434b-9d87-43fe70045754`), producing kb.ingest_items row `738496ed-c365-4333-b1f4-6b0388541689` with 1 chunk/entry (earlier runs `b8959bbb-...` etc. remain for history).
- KB search tool run (`flow_run_id=26387b21-0ac1-4103-af78-204ca185e267`) confirms the new entry is returned; manual SQL (`kb.search_entries('Freshbot live ingestion')`) shows snippet "Freshbot live ingestion check …".
- Gemini chat tool run (`flow_run_id=1518d32c-6814-4b19-b676-1af4a8c2bd50`) now completes after forwarding the LiteLLM virtual key via connector headers (“Hi there.”).
- ParadeDB has been refreshed with the latest registry bundles (connectors, tools, agents, prompts) and Prefect deployments re-applied on the `freshbot-process/freshbot-default` pool + queue.
- The dedicated `prefect-worker` compose service is running (`docker compose ps prefect-worker`) so flow runs drain automatically without manual CLI commands.
- Code embeddings are stubbed by default; real Ollama calls stay disabled until `FRESHBOT_ENABLE_CODE_EMBEDDINGS=1` is exported.
- `kb.entries` now carries `is_document`, `is_note`, `needs_metadata`, and `is_chat` flags (document ingests set `is_document=true`, chat transcripts set `is_chat=true`), and supplemental annotations belong in the new `kb.entry_metadata` table.

## Completed Recently
- Re-ran the registry and Prefect loaders manually (`docker compose exec api …`) after the latest changes.
- Re-applied Prefect deployments from `src/freshbot/flows/flows.yaml` and verified they list under `prefect deployment ls`.
- Updated `docker-compose.yml` so the api container exports `PREFECT_RESULTS_PERSIST_BY_DEFAULT=true` and `PREFECT_LOCAL_STORAGE_PATH=/tmp/prefect-results`, meaning flow results persist across restarts without manual worker flags.
- Restarted the API container, updated agent validation (allow `application/json` mime type, enforce minimum thinking budgets), and confirmed `/freshbot/flows/execute` surfaces Prefect states cleanly.
- Increased the Qwen connector timeout to 240s to match the proxy expectations and documented the change in `freshbot_phase0.md`.
- Validated the Qwen tool deployment via `/freshbot/flows/execute` using the correct Prefect slug (`freshbot_tool_qwen_chat/freshbot-tool-qwen-chat`).
- Ran `freshbot_document_ingest` through Prefect (Base64 upload) and confirmed database writes to `kb.ingest_items`, `kb.chunks`, and `kb.entries` (latest ingest item `738496ed-c365-4333-b1f4-6b0388541689`; earlier runs `632b399b-d561-4114-848e-67c38177d891` and `b7e3a2ba-...` retained for provenance).
- Triggered `freshbot_tool_kb_search` via Prefect and validated returned snippets against the newly ingested document by querying `kb.search_entries` (flow run `26387b21-0ac1-4103-af78-204ca185e267`).
- Patched the OpenAI gateway to honour connector-defined headers (LiteLLM Authorization) and re-ran `freshbot_tool_gemini_chat` to confirm a full end-to-end success.
- Added API-level coverage for `/freshbot/flows/execute` (FastAPI TestClient with Prefect mocks) so success, result, and ObjectNotFound cases stay guarded even when Prefect is stubbed.
- Added a `prefect-worker` docker-compose service so the Prefect worker launches automatically with the correct environment and queue bindings.
- Stubbed `tool_code_embed`/Ollama gateway responses (flow `e9f16c18-f520-44f2-bd12-d65a9b10f11d`) returning zero vectors + warning while the embedding model remains offline; documented the `FRESHBOT_ENABLE_CODE_EMBEDDINGS` switch.
- Wired metadata flag handling: ingestion now pushes `meta_flags` (e.g. `needs_metadata`) into the stub Prefect deployment `freshbot_metadata_flag` (latest run `649f102e-27be-40af-b847-f7ba65ef9192`).
- Extended the ParadeDB schema with entry classification flags and the `kb.entry_metadata` table; reran `ops/scripts/migrate.py` so the live database matches the new definition.
- Executed the focused pytest suite covering the gateway/runtime/executor stack.

## Immediate Next Actions
- Automate the registry + deployment refresh on compose restarts (script or devtool wrapper) to reduce manual recovery steps (still manual today).
- Extend smoke validation to the Gemini tool and the document-ingest flow once embeddings and planners are ready.
- Capture API-level tests for `/freshbot/flows/execute` covering failed Prefect states and result persistence gaps (include the new Qwen timeout path).
- Monitor the `prefect-worker` service (`docker compose logs -f prefect-worker`) and restarts to ensure it stays attached to `freshbot-process` after upgrades.

## Upcoming / Backlog
- Continue the ingestion/agent refactor so every summarisation/classification/embedding step is driven by the ParadeDB registry schemas.
- Seed ParadeDB with the KB metadata/notes tables described in `project-docs/instructions.md` and wire them into the registry loader bundles.
- Expand automated coverage for the FastAPI executor route and end-to-end Prefect invocations once the worker path is proven.
- Build repeatable smoke scripts in `src/freshbot/devtools/` for invoking agents/tools and capturing snapshots of the registry state.

## Open Questions / Decisions
- How should non-404 Prefect errors (auth, worker offline, parameter validation) be surfaced to API clients—raw Prefect messages or structured problem details?
- When do we want to schedule the ParadeDB seeding (during compose boot, via CI job, or controlled manual step) to keep runtime config aligned with git.
