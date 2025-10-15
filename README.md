# My Agentic Chatbot

Container-first agentic runtime that ingests documents into ParadeDB, enriches them with NER/emotion/embeddings, and serves planner-driven chat on top of the knowledge base.

## Quick start

1. Copy `.env.example` to `.env`, fill in provider keys, and keep the ParadeDB host as `paradedb` when running inside Docker.
2. Launch the stack: `docker compose up -d`.
3. Exec into the `api` container for anything that touches the database or runtime config:
   ```bash
   docker compose exec api bash
   ```
4. Smoke checks from inside `api`:
   - `prefect version` (client reachable)
   - `python ops/scripts/ingest_docs.py --use-live project-docs/instructions.md`
   - `psql $DATABASE_URL -c "SELECT extname FROM pg_extension WHERE extname = 'pg_search';"`

Full service details, ingestion flow, and troubleshooting live under `project-docs/`:

- `project-docs/project.md` – stack overview and container workflow
- `project-docs/runtime-config.md` – cfg tables, active model/tool aliases, BM25 requirements
- `project-docs/ingestion-flow.md` – end-to-end document intake and verification checklist
