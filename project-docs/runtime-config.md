# Runtime Configuration & Embedding Pipeline

This stack now resolves every model/tool endpoint from `cfg.*` tables in Postgres. The application refuses to boot if a required key is missing or a referenced model/tool is disabled.

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

## Resetting kb/agent schemas

Use the helper script to drop and recreate knowledge-base tables without touching `cfg.*`:

```bash
python ops/scripts/reset_kb.py          # drop + rebuild schemas
python ops/scripts/reset_kb.py --drop-only  # drop only
python ops/scripts/reset_kb.py --indexes-only  # rebuild embedding indexes only
```

The script executes the non-`cfg` statements from `src/my_agentic_chatbot/storage/models.sql` and leaves existing runtime configuration untouched. Per-space HNSW DDL is defined in `ops/scripts/sql/create_embedding_indexes.sql`; both `reset_kb.py` and the `--indexes-only` mode resolve the active embedding space names to ids before creating chunk/document indexes.

## Embedding services

- LiteLLM aliases (`emb-general`, `emb-legal`, `emb-code`) target TEI containers via environment variables:
  - `TEI_GTE_LARGE_URL`
  - `TEI_LEGAL_BERT_URL`
- `emb-code` defaults to the general encoder; override the target by updating `cfg.models` and LiteLLM config if you provision a dedicated code encoder.
- The TEI checkpoints are **not stored in git**. Rebuild them locally with `python ops/tei/wrap_models.py` (see script help for inputs) and place the outputs under `models/` before starting the stack.

Ensure the TEI images exist locally:

```bash
docker pull ghcr.io/huggingface/text-embeddings-inference:cpu-1.6
docker tag ghcr.io/huggingface/text-embeddings-inference:cpu-1.6 ghcr.io/huggingface/text-embeddings-inference:cpu-0.7
```

## Smoke tests

1. `docker compose up -d tei-gte-large tei-legal` (or the full stack).
2. `curl -s http://localhost:11434/api/tags | jq` – verify the Ollama classifiers.
3. `curl -s http://localhost:4000/v1/embeddings -H "Authorization: Bearer $LITELLM_VIRTUAL_KEY" -d '{"model":"emb-general","input":["hello"]}' | jq '.data[0].embedding | length'`
4. `pytest tests/test_intake_pipeline.py` – validates ingestion flows with the runtime-config stubs.

## Clean-slate acceptance run

When resetting ParadeDB, follow this sequence:

1. `python ops/scripts/reset_kb.py` – drops/recreates `kb` and `agent` schemas while leaving `cfg.*` intact.
2. `docker compose restart litellm` – reloads LiteLLM aliases after any config/env changes.
3. Smoke-test LiteLLM embeddings:
   - `model=emb-general` returns 1024-length vectors.
   - `model=emb-legal` returns 768-length vectors.
   - `model=emb-code` matches the encoder bound in `cfg.active`.
4. Ingest a small sample corpus and run `python ops/scripts/embed_chunks.py --space $(psql -XtAc "SELECT value FROM cfg.active WHERE key='active_emb_general'")` to validate ingestion → embedding → persistence.
5. `python ops/scripts/reset_kb.py --indexes-only` – rebuilds per-space HNSW indexes once the first embeddings land.
6. Run the full embedding workload (Prefect flow or `embed_chunks.py` across each active space).
7. Verify per-space counts and norms (`SELECT COUNT(*) FROM kb.chunk_embeddings WHERE space_id = ...`) and confirm indexes (`\di idx_chunk_embeddings_*`, `\di idx_document_embeddings_*`).

## Logging

Embedding and search calls now emit structured logs with resolved space names and vector dimensions:

```
chat embeddings generated {"spaces_used": ["emb-general", "emb-legal"], "dimensions": {"emb-general": 1024, "emb-legal": 768}}
search toolbox resolved embedding spaces {"spaces_used": ["emb-general", "emb-legal"], "k_per_space": {"emb-general": 6, "emb-legal": 6}}
```

These logs originate from `chat_store`, `web_ingest`, the web search toolbox, and the Prefect `embed_chunks` task.
