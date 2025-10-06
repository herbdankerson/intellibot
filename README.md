# My Agentic Chatbot

This repository bootstraps a modular agentic chatbot that follows the architecture
outlined in `instructions.md`. It now wires the runtime stack described in the project
contract:

- LiteLLM is the single entrypoint for planner/responder/embedding calls.
- The workflow orchestrator runs as a Prefect flow with approvals at plan, task, and
  final-response checkpoints.
- Database tools communicate with the Postgres MCP server over FastMCP SSE transport.
- Docker Compose spins up ParadeDB/Postgres, LiteLLM, Prefect, OpenWebUI, and the
  Postgres MCP sidecar.

## Quick start

1. **Install dependencies** (for local scripts/tests):

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Run services**:

   ```bash
   docker compose up -d
   ```

   This provisions ParadeDB, LiteLLM, Prefect (`http://localhost:4200`), OpenWebUI
   (`http://localhost:3000`), the Postgres MCP server (`http://localhost:4050`), and
   a SearXNG metasearch instance (`http://localhost:8085`).

3. **Apply the schema and seed content**:

   ```bash
   python ops/scripts/migrate.py
   python ops/scripts/ingest_docs.py docs
   python ops/scripts/embed_chunks.py --space emb-general
   ```

4. **Run the FastAPI app (optional)**:

   ```bash
   uvicorn src.my_agentic_chatbot.main:app --host 0.0.0.0 --port 8000
   ```

### SearXNG search service

SearXNG boots with the rest of the stack and is also runnable on its own:

```bash
docker compose up -d searxng searxng-redis
```

The instance listens on `http://localhost:8085`. You can issue a quick smoke
test with:

```bash
curl -H 'User-Agent: Mozilla/5.0' 'http://localhost:8085/search?q=test'
```

Set `SEARXNG_SECRET` (and optionally `SEARXNG_BASE_URL`) in `.env` before
exposing the service beyond localhost.

## Development

Install dependencies and run the test suite:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest
```

The test suite now covers the Prefect workflow, MCP client normalization, and the
LiteLLM-backed planner/responder adapters.

## Running the API

```bash
uvicorn src.my_agentic_chatbot.main:app --reload
```
