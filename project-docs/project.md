# Project Overview

This repository hosts the agentic chatbot runtime that now relies on the Microsoft Agent Framework for orchestration.  The stack is container-first: all runtime services are meant to be launched through Docker Compose, while the Python virtual environment is only needed for local testing and utility scripts.

## Running the stack

1. Copy `.env.example` to `.env` and fill in provider keys (e.g., `GOOGLE_API_KEY`, `LITELLM_VIRTUAL_KEY`).
2. Start the services from the repository root:

   ```bash
   docker compose up -d
   ```

   The compose file builds the API image from the local source tree and mounts the repo into the container for live reloads.

3. Confirm the API is reachable at `http://localhost:8800/health`.  LiteLLM is exposed on `http://localhost:4000`, and the SearxNG instance is available at `http://localhost:8085`.

### Key services

| Service | Purpose | Host port |
| --- | --- | --- |
| `api` | FastAPI app running the Agent Framework workflow | 8800 |
| `paradedb` | Primary Postgres + pgvector store for KB and staging tables | 5432 |
| `litellm` | Gateway for model calls (chat + embeddings) | 4000 |
| `openai-proxy` | Local OpenAI-compatible proxy in front of LiteLLM | 5001 |
| `prefect` | Prefect Orion API for ETL flows | 4200 |
| `openwebui` | Browser UI that talks to the proxy, optional | 3000 |
| `searxng` | Metasearch engine feeding the web curation pipeline | 8085 |
| `docling` | Document conversion microservice used by ingestion | 8000 |

Additional MCP sidecars (Playwright, ParadeDB, Neo4j) start with the same command; see `docker-compose.yml` for the full inventory and exposed ports.

## Database details

- **Engine**: ParadeDB (Postgres 15 + pgvector)
- **Connection string**: `postgresql://agent:agentpass@localhost:5432/agentdb`
- **Schema highlights**:
  - `kb.documents`, `kb.chunks`, `kb.document_embeddings`, `kb.chunk_embeddings`
  - `agent.web_work_items` staging table for curated web captures
  - `agent.events` for LiteLLM call logs

### Migrations and seeding

Run the helper scripts from the host machine (requires the virtual environment):

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python ops/scripts/migrate.py
python ops/scripts/ingest_docs.py path/to/docs
python ops/scripts/embed_chunks.py --space emb-general
```

Re-run the embedding script whenever new documents land in the KB.

## Development workflow

- Use the containers for runtime tasks.  The local virtual environment is optional and only needed when running unit tests or maintenance scripts.
- Run tests with `pytest` (either inside the `api` container or from the host after activating the venv).
- Logs from model calls are captured in `agent.events`; other service logs can be tailed with `docker compose logs -f <service>`.

## Credentials and access

The default credentials defined in `docker-compose.yml` are intended for local development only:

- ParadeDB/Postgres: user `agent`, password `agentpass`, database `agentdb`
- Neo4j: set `NEO4J_USER`, `NEO4J_PASSWORD`, and `NEO4J_DATABASE` in `.env`
- LiteLLM: `LITELLM_MASTER_KEY` and `LITELLM_VIRTUAL_KEY` default to `changeme-*` placeholders

Always override these values in production and restrict exposed ports if the stack is deployed beyond localhost.
