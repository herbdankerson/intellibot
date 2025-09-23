# My Agentic Chatbot

This repository bootstraps a modular agentic chatbot that follows the architecture
outlined in `instructions.md`. It includes:

- A FastAPI application with `/health` and `/run` endpoints.
- Planner, workflow orchestrator, and responder components.
- Lightweight database, graph, and web tool adapters.
- Ops scripts for migrations, ingestion, and embedding jobs.
- Configuration for LiteLLM and MCP servers plus JSON Schemas for validation.
- Unit tests covering the core planner, orchestrator, tools, and responder behavior.

## Development

Install dependencies and run the test suite:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest
```

## Running the API

```bash
uvicorn src.my_agentic_chatbot.main:app --reload
```
