# Legal Document ETL Pipeline

This project ingests legal PDFs, converts them to Markdown with Docling, builds adaptive chunks, adds lightweight legal NER tags, requests embeddings through the LiteLLM proxy (Voyage pool by default), and stores everything in ParadeDB (Postgres + pgvector + BM25).

## 1. Setup

```bash
cd mcp/legal-knowledge-base
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python - <<'PY'
import nltk
nltk.download('punkt')
PY
```

Copy `.env` and fill in the ParadeDB connection details. Set `LITELLM_BASE_URL` if the proxy is not running on the default `http://localhost:4000/v1`, and populate any LiteLLM auth if you enable it (`LITELLM_API_KEY`, etc.).

## 2. ParadeDB Schema

Update the connection values in `.env`, then apply the schema (installs `vector`
and `pg_search`, plus creates the PDF + web ingestion tables):

```bash
psql "host=$PGHOST port=$PGPORT dbname=$PGDATABASE user=$PGUSER password=$PGPASSWORD" -f db/schema.sql
```

## 3. Ingest Documents

Drop PDFs into `data/inbox/` and run either:

```bash
python -m src.ingest_one data/inbox/sample.pdf case_filing
# or batch
python -m src.ingest_batch
```

Parsed Markdown + metadata lands in `data/parsed/`.

### Versioning & Deduplication

- Each ingest stores a new `docs` version (per `slug`). If the markdown matches an existing version exactly, the run aborts without touching the database.
- When a file differs, the pipeline auto-increments `docs.version` and stamps every chunk with the matching `doc_version`/`version` so you can query historical revisions.
- Re-ingesting an updated brief therefore keeps prior versions intact; rerunning an identical file is a no-op.

### Ingest Web Sources

Use the scraper to pull statutes, regulations, or guidance pages straight into the new `web` / `web_chunks` tables:

```bash
python -m src.ingest_web "https://www.flsenate.gov/Laws/Statutes/2024/61.403"
```

The script fetches HTML, normalises it to Markdown, deduplicates against prior versions of the same `slug`, and stores both page-level and chunk-level embeddings alongside NER tags. Duplicate content (byte-for-byte) is skipped automatically; revised content increments the `version` column for the page and its chunks. If you need to reset a source, delete its `web` row (and cascaded chunks) and rerun the command to mint a fresh version 1.

## 4. Search Demo

```bash
python -m src.search_demo "relocation under Florida law 61.13001"
```

The script prints the top chunks ranked by the LiteLLM-served legal embedding alias (defaults to Voyage `voyage-law-2`).

## 5. LiteLLM Key Rotation

- LiteLLM pulls credentials from `litellm/config.yaml`; update the Voyage/Google keys there whenever you rotate them. Duplicate `model_name` entries form the failover pool when a key hits rate/budget limits.
- The bundled LiteLLM proxy mounts a patched `litellm/main.py` that round-robins Voyage keys and falls back to the next entry on failure. If you rebuild the container images, copy those overrides back before ingesting.
- The config file is mounted read-only into the `litellm` container; restart the stack after editing so the proxy picks up changes.
- By default the pipeline calls `voyage-2` and `voyage-law-2`. Keep their replacements at 1024 dimensions if you modify the ParadeDB schema.

## 6. MCP Connector (FastAPI)

- The bridge now ships as the `legal-mcp` service in the root Docker Compose stack. Start it with `docker compose up -d legal-mcp` (or include it when bringing the full stack up).
- The container listens on `http://localhost:8100` (port 8000 inside the Docker network); the manifest lives at `http://localhost:8100/.well-known/mcp.json`.
- Manual dev remains supported: `uvicorn src.mcp_server:app --host 0.0.0.0 --port 8000` from a local virtualenv mirrors the container build.
- Register the connector inside ChatGPT / GPT Pro pointing to the Cloudflare tunnel or local URL, e.g. `https://<tunnel-hostname>/.well-known/mcp.json`. The server exposes the following tools:
  - `ingest_websites`: scrape one or more URLs and load them into the versioned `web` tables with embeddings.
  - `search`: query ParadeDB for the top semantic matches across PDF (`docs`) and scraped (`web`) sources.
- Health endpoint: `GET /healthz`. Manifest: `/.well-known/mcp.json` (redirects to the MCP mount).
- The container reads `MCP_PUBLIC_ENDPOINT` (set in Compose) to advertise the externally reachable `/mcp` URL in the manifest. Update it if you change domains or tunnel hosts.
- The FastAPI route `/mcp/tools` returns the currently registered MCP tool names for quick sanity checks.

## Next Steps

- Tune chunk sizes by changing `CHUNK_TOKENS` / `CHUNK_OVERLAP_MAX_PCT` in `.env`.
- Layer in reranking (e.g., Voyage `rerank-2.5`) before presenting results.
- Wrap ingestion/search in a lightweight API if you want remote access or MCP integration.
