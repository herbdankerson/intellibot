# Project Overview

Agentic chatbot runtime prefect orchestration and hybrid BM25/vector search over ParadeDB. Everything assumes you are operating from inside the Docker Compose stack.

## Running the stack

1. Copy `.env.example` to `.env` and populate provider/API keys. Leave the database host set to `paradedb` unless you are deliberately pointing at an external Postgres instance.
2. Start services from the repo root:

   ```bash
   docker compose up -d
   ```

   The compose build mounts the source tree into the `api` container for live reloads.

3. Shell into `api` for all runtime commands (tests, Prefect flows, psql, scripts):

   ```bash
   docker compose exec api bash
   ```

4. Confirm key services:
   - API health: `curl -f http://localhost:8800/health`
   - ParadeDB reachable and `pg_search` loaded: `psql $DATABASE_URL -c "SELECT extname FROM pg_extension WHERE extname = 'pg_search';"`
   - LiteLLM embeddings: `python ops/scripts/check_embeddings.py --model emb-general` (see script help)

### Key services

| Service | Purpose | Host port |
| --- | --- | --- |
| `api` | FastAPI runtime + Prefect tasks | 8800 |
| `paradedb` | Postgres + pgvector + pg_search (BM25) | 5432 |
| `litellm` | Chat + embedding proxy | 4000 |
| `openai-proxy` | OpenAI-compatible shim over LiteLLM | 5001 |
| `prefect` | Prefect Orion API | 4200 |
| `docling` | Document conversion microservice | 8000 |
| `searxng` | Metasearch for web enrichment | 8085 |
| `openwebui` | Optional browser UI | 3000 |

Additional MCP sidecars (Playwright, ParadeDB MCP, Neo4j) are defined in `docker-compose.yml` and start with the same command.

## Container-first workflow

- Always execute scripts/tests inside the `api` container so DNS (`paradedb`) and shared volumes resolve correctly.
- Use the shared connection helper (`src/my_agentic_chatbot/storage/connection.py`) whenever code needs ParadeDB access. Direct DSNs in modules are prohibited.
- Prefect flows, ingestion scripts, and runtime services share the same environment variables sourced from `.env`.

## Database details

- **Engine**: ParadeDB (Postgres 15, pgvector, pg_search)
- **Internal connection string**: `postgresql://agent:agentpass@paradedb:5432/agentdb`
- **Mandatory extensions**: `vector`, `pg_search` (BM25). The bm25 index lives in the `pg_search` extension; queries rely on `paradedb.match`/`paradedb.score`.
- **Core schemas**:
  - `kb.ingest_items` – tracking of ingestion runs and status
  - `kb.documents`, `kb.chunks` – versioned canonical content + chunk breakdowns
  - `kb.document_embeddings`, `kb.chunk_embeddings` – per-space vectors
  - `kb.entries` – hybrid search surface (linked back to documents/chunks)
  - `cfg.*` – runtime model/tool/agent configuration

Ingestion, deduplication, and re-run semantics are captured in `project-docs/ingestion-flow.md`.

## Development workflow

- Preferred path: `docker compose exec api pytest` or run Prefect tasks from inside the container. Host virtualenvs are optional and should only be used when you intentionally target remote infrastructure.
- Logs: application (`docker compose logs -f api`), LiteLLM (`litellm` service), Prefect (`prefect` service), Docling (`docling` service).
- Database troubleshooting: `psql $DATABASE_URL` inside `api`. The schema auto-patches required columns on first ingest run.

## Credentials and access

The compose file ships with development-only defaults:

- ParadeDB/Postgres: `agent` / `agentpass` / `agentdb`
- LiteLLM master/virtual keys: `changeme-*`
- Neo4j placeholders in `.env`

Override these values for any non-local deployment and restrict exposed ports as needed.
