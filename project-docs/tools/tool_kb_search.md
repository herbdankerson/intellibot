# Tool: `tool_kb_search`

## Overview
- Hybrid BM25 + vector search across `kb.entries` via ParadeDB helper `kb.search_entries`.
- Returns ranked snippets with source URIs, scores, and optional summaries for downstream planners.
- Designed as the primary entry point for research and orientation; accepts free-form search text.

## Inputs
- `query` *(string, required)* – Natural language or keyword query to evaluate.
- `limit` *(integer, optional, default 5)* – Maximum number of entries to return.
- `min_score` *(number, optional)* – Minimum combined hybrid score; items below threshold are dropped.

## Outputs
- `results` *(array)* – Each element contains:
  - `entry_id` *(UUID string)* – ParadeDB row identifier for the chunk.
  - `source` *(string)* – Logical scope or ingest source.
  - `uri` *(string)* – Canonical path/URL where the chunk originated.
  - `title` *(string)* – Chunk title or document heading when available.
  - `snippet` *(string)* – Search-highlighted excerpt.
  - `text_score`, `vec_score`, `score` *(numbers)* – Lexical, vector, and blended scores.
  - `summary` *(string, optional)* – Chunk summary when stored during ingestion.

## Usage Notes
- Ideal as the first step when planning tasks; combine with `tool_manifest_fetch` or `tool_agent_catalog` to pivot into actionable flows.
- Setting `min_score` helps filter noise when precise matches are required (e.g., 0.15 for narrow filters, 0.05 for exploratory searches).
- Results include `entry_id`, which can be passed to scoped Neo4j or document fetch tools.
- For dev-only docs, check `is_dev` flags returned by Codex MCP wrappers.

## Related Tools & Flows
- `tool_search_tools` – Discover additional tooling once relevant topics are identified.
- `tool_manifest_fetch` – Retrieve quick-start docs for any tool discovered via search.
- `freshbot-tool-prefect-catalog` flow – Surfaces Prefect deployments that can be invoked with the Prefect execution tool.

