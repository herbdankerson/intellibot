# Ingestion Runbook

Use this checklist whenever you push documents through the live ParadeDB pipeline. All LLM-heavy steps (classification, summaries, emotion analysis) now call into the Freshbot tool registry — the flow invokes `tool_qwen_chat` and `tool_gemini_chat` via `freshbot.pipeline.ingestion`, so ParadeDB centrally manages prompts, models, and schema validation.

All commands assume `docker compose exec api bash`.

## 1. Pre-flight

- `printenv DATABASE_URL` → should resolve to `postgresql://agent:agentpass@paradedb:5432/agentdb`.
- `psql $DATABASE_URL -c "SELECT extname FROM pg_extension WHERE extname = 'pg_search';"` → confirms ParadeDB's BM25 extension is loaded.
- Ensure LiteLLM embeddings are reachable: `python ops/scripts/check_embeddings.py --model emb-general` (expect 1024-dimensional vectors).
- Optional sanity check: `curl -s http://tei-gte-large/info | jq '.max_input_length'` should report `512`, matching the 450-token chunk budget.

## 2. Launch ingest

```
python ops/scripts/ingest_docs.py --use-live path/to/doc.md
```

- `--use-live` points the flow at ParadeDB and LiteLLM.
- Use `--job-name <slug>` to make logs easier to search in Prefect.

## 3. Monitor

- Tail logs: `prefect task-run ls --state-name Running` then `prefect task-run logs <id>`.
- ParadeDB writes: `docker compose logs -f paradedb`.
- LiteLLM throttling shows up in `docker compose logs -f litellm` – expect occasional retries but no hard failures.

## 4. Post-run verification

Run the following SQL bundle:

```
psql $DATABASE_URL <<'SQL'
\timing
SELECT job_id, status, count(*)
FROM kb.ingest_items
GROUP BY job_id, status
ORDER BY job_id DESC;

SELECT count(*) AS doc_count,
       sum(case when version > 1 then 1 else 0 end) AS doc_revisions
FROM kb.documents;

SELECT count(*) AS chunk_count,
       sum(case when version > 1 then 1 else 0 end) AS chunk_revisions
FROM kb.chunks;

SELECT count(*) AS chunk_embeddings,
       count(*) FILTER (WHERE space_id = s.id) AS general_embeddings
FROM kb.chunk_embeddings,
     LATERAL (SELECT id FROM kb.embedding_spaces WHERE name = 'emb-general') AS s;

SELECT count(*) AS entry_rows,
       sum(case when embedding IS NOT NULL then 1 else 0 end) AS embedded_entries
FROM kb.entries;

SELECT is_document,
       is_note,
       needs_metadata,
       count(*)
FROM kb.entries
GROUP BY is_document, is_note, needs_metadata
ORDER BY is_document DESC, is_note DESC;

SELECT entry_id, meta_type
FROM kb.entry_metadata
ORDER BY created_at DESC
LIMIT 5;
SQL
```

Expected outcomes:

- `kb.ingest_items.status` should be `completed` for the new job.
- `kb.documents` includes the new `source_uri` with correct `version` increments.
- `kb.chunk_embeddings` count matches `kb.chunks` for the active embedding space.
- `kb.entries` shows populated `embedding`, `summary`, `ner`, and `emotions` fields, with document rows flagged (`is_document=true`), chat transcripts marked (`is_chat=true`), and parser failures surfaced via `needs_metadata=true`.
- `kb.entry_metadata` remains empty unless you've seeded annotations for specific entry UUIDs.
- When `meta_flags` appear in the stored metadata (e.g. `needs_metadata`), the stub Prefect deployment `freshbot-metadata-flag` runs automatically; check Prefect runs for the flag name and entry id.

## 5. Spot checks

- Sample hybrid search: `psql $DATABASE_URL -c "SELECT id, score FROM kb.search_entries('test', NULL, 5);"`
- Direct pg_search probe: `psql $DATABASE_URL -c "SELECT paradedb.score(id) FROM kb.entries WHERE id @@@ paradedb.match('content', 'test') LIMIT 5;"`
- Inspect a document: `psql $DATABASE_URL -x -c "SELECT * FROM kb.documents ORDER BY updated_at DESC LIMIT 1;"`
- Ensure emotion payload exists: `psql $DATABASE_URL -c "SELECT emotions->>'primary' FROM kb.entries ORDER BY updated_at DESC LIMIT 5;"`

## 6. Cleanup / reruns

- Re-run the same command to backfill partial ingests. The pipeline reuses matching chunks and embeddings automatically.
- To rebuild from scratch: `python ops/scripts/reset_kb.py` followed by the acceptance run in `project-docs/runtime-config.md`.

## 7. Escalations

- BM25 missing → reinstall `pg_search` or rebuild ParadeDB image.
- LiteLLM persistent 429s → adjust rate limits in `ops/litellm/config.yaml` and redeploy the `litellm` service.
- Prefect flow stuck → `prefect deployment run ingest-document/live --params '{"ingest_item_id": "..."}'` to resume from the Prefect UI or CLI.

Keep this runbook close; update it whenever the pipeline changes.
