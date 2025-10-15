# Runtime Configuration & Embedding Pipeline

This stack now resolves every model/tool endpoint from `cfg.*` tables in ParadeDB. The application refuses to boot if a required key is missing or a referenced model/tool is disabled, and it requires the BM25 primitives provided by the `pg_search` extension.

## Active configuration

```sql
SELECT key, value FROM cfg.active;  -- required keys: active_planner_model, active_responder_model,
                                   -- active_worker_model, active_emb_general, active_emb_legal,
                                   -- active_emb_code
SELECT name, provider, identifier, dims FROM cfg.models ORDER BY name;
SELECT name, resolved_endpoint FROM cfg.tools;
```

Planner/responder/worker aliases resolve to LiteLLM model names `planner`, `responder`, and `cheap-worker`, all of which currently point at the local `smollm2:1.7b` service defined in `ops/litellm/config.yaml`.

Update the running models by editing `cfg.models`/`cfg.active` – no code changes required. Example:

```sql
UPDATE cfg.models SET identifier = 'planner-v2' WHERE name = 'planner-local-smollm';
UPDATE cfg.active SET value = 'planner-local-smollm' WHERE key = 'active_planner_model';
```

All application modules must acquire ParadeDB handles via `src/my_agentic_chatbot/storage/connection.py`; the helper normalises DSNs for both psycopg and SQLAlchemy users and keeps connection pooling consistent across the runtime and Prefect flows.

## Resetting kb/agent schemas

Use the helper script to drop and recreate knowledge-base tables without touching `cfg.*`:

```bash
python ops/scripts/reset_kb.py          # drop + rebuild schemas
python ops/scripts/reset_kb.py --drop-only  # drop only
python ops/scripts/reset_kb.py --indexes-only  # rebuild embedding indexes only
```

The script executes the non-`cfg` statements from `src/my_agentic_chatbot/storage/models.sql` and leaves existing runtime configuration untouched. Per-space HNSW DDL is defined in `ops/scripts/sql/create_embedding_indexes.sql`; both `reset_kb.py` and the `--indexes-only` mode resolve the active embedding space names to ids before creating chunk/document indexes.

### Entry classification & metadata

- `kb.entries` now carries `is_document`, `is_note`, `needs_metadata`, and `is_chat` booleans. The live document pipeline sets `is_document=true` on ingest, `is_chat=true` for chat transcripts, and toggles `needs_metadata` when doc parsing fails (for example, missing Docling output).
- Structured add-ons live in `kb.entry_metadata` (`entry_id`, `meta_type`, `data`). Use this table for ancillary annotations rather than overloading the base `meta` JSON field.
- Any `meta_flags` emitted during ingestion (for example, `needs_metadata`) fan out to the stub Prefect deployment `freshbot-metadata-flag`, which currently logs the request and returns immediately.

## Embedding services

- LiteLLM aliases (`emb-general`, `emb-legal`) target the TEI containers via:
  - `TEI_GTE_LARGE_URL`
  - `TEI_LEGAL_BERT_URL`
- Code embeddings are served by the dedicated `qwen3-embed` Vulkan container (`http://qwen3-embed:8080/v1/embeddings`). The connector entry `connector_ollama_code_embedding` now points at that OpenAI-compatible endpoint; update `cfg.models` via `ops/scripts/sql/20251014_refresh_code_embed_connector.sql` if you need to redeploy the stack.
- **Stub mode:** until the Ollama embedding model is certified, `freshbot` returns zero vectors by default. Set `FRESHBOT_ENABLE_CODE_EMBEDDINGS=1` (and restart the API/worker) to issue real embedding calls against the gateway.
- The TEI checkpoints are **not stored in git**. Rebuild them locally with `python ops/tei/wrap_models.py` (see script help for inputs) and place the outputs under `models/` before starting the stack.
- The `/info` endpoint on `tei-gte-large` reports `max_input_length = 512`; our chunker requests 450 rough tokens (~88 %) to keep plenty of headroom and avoid HTTP 413 responses.

Ensure the TEI images exist locally:

```bash
docker pull ghcr.io/huggingface/text-embeddings-inference:cpu-1.6
docker tag ghcr.io/huggingface/text-embeddings-inference:cpu-1.6 ghcr.io/huggingface/text-embeddings-inference:cpu-0.7
```

## Smoke tests (run inside `docker compose exec api`)

1. `prefect version` – verifies the client can reach the Prefect API.
2. `python ops/scripts/ingest_docs.py --use-live project-docs/instructions.md` – exercises the live embedding pipeline in ParadeDB.
3. `psql $DATABASE_URL -c "SELECT paradedb.score(id) FROM kb.entries WHERE id @@@ paradedb.match('content', 'test') LIMIT 1;"` – confirms `pg_search` text search is live.
4. `python -m pytest tests/test_intake_pipeline.py` – validates ingestion logic against the runtime-config stubs (no direct ParadeDB mutations).

## Clean-slate acceptance run

When resetting ParadeDB, follow this sequence (all steps from inside `api`):

1. `python ops/scripts/reset_kb.py` – drops/recreates `kb` and `agent` schemas while leaving `cfg.*` intact.
2. `psql $DATABASE_URL -c "SELECT extname FROM pg_extension WHERE extname = 'pg_search';"` – verify the extension stayed loaded.
3. `docker compose restart litellm` – reloads LiteLLM aliases after any config/env changes.
4. Smoke-test embeddings (see commands above). Expect 1024 dims for `emb-general`, `emb-legal`, and `emb-code` (the Qwen2.5-based encoder trims to the 1024-dim ParadeDB space).
5. Ingest a sample corpus with `python ops/scripts/ingest_docs.py --use-live project-docs/instructions.md` and confirm rows via the runbook in `project-docs/ingestion-flow.md`.
6. `python ops/scripts/reset_kb.py --indexes-only` – rebuilds per-space HNSW indexes once the first embeddings land.
7. Run the full embedding workload (Prefect flow or `embed_chunks.py` across each active space) and verify counts in `kb.chunk_embeddings` and `kb.document_embeddings`.

## Logging

Embedding and search calls now emit structured logs with resolved space names and vector dimensions:

```
chat embeddings generated {"spaces_used": ["emb-general", "emb-legal"], "dimensions": {"emb-general": 1024, "emb-legal": 1024}}
search toolbox resolved embedding spaces {"spaces_used": ["emb-general", "emb-legal"], "k_per_space": {"emb-general": 6, "emb-legal": 6}, "full_text": "pg_search"}
```

These logs originate from `chat_store`, `web_ingest`, the web search toolbox, and the Prefect `embed_chunks` task.


### Freshbot agent/tool runtime
- Call `freshbot.executors.invoke_agent()` / `invoke_tool()` from the API layer to run Prefect-backed flows using the metadata stored in ParadeDB.
- Agent defaults (entrypoints, prompts, gateway aliases) live in `cfg.agents.params`; override per-call behaviour by passing keyword arguments in the payload.
- Tool input/output schemas are captured in `cfg.tools.default_params` and exposed in the runtime result metadata for validation.
