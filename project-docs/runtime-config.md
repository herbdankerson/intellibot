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
```

The script executes the non-`cfg` statements from `src/my_agentic_chatbot/storage/models.sql` and leaves existing runtime configuration untouched.

## Embedding services

- LiteLLM aliases (`emb-general`, `emb-legal`, `emb-code`) target TEI containers via environment variables:
  - `TEI_GTE_LARGE_URL`
  - `TEI_LEGAL_BERT_URL`
- `emb-code` defaults to the general encoder; override the target by updating `cfg.models` and LiteLLM config if you provision a dedicated code encoder.

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

## Logging

Embedding calls now emit structured logs with resolved space names and vector dimensions:

```
chat embeddings generated {"spaces_used": ["emb-general", "emb-legal"], "dimensions": {"emb-general": 1024, "emb-legal": 768}}
```

These logs originate from `chat_store`, `web_ingest`, and the Prefect `embed_chunks` task.
