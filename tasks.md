Below is a step-by-step guide for turning the current scaffold into a functional agentic chatbot.  Each step lists the file(s) to create or modify and outlines the key code you or your IDE should implement.  Where possible, I refer back to the existing `instructions.md` specification for context.

---

## 1. Define schemas (Pydantic models)

**File:** `src/my_agentic_chatbot/schemas.py`

Create Pydantic data classes for the core data contracts you described in the instructions:

* `Task`: fields `id: str`, `intent: str`, `tool: Literal['db_keyword','db_vector','graph','web']`, `inputs: dict`, `budget_tokens: int`, `timeout_s: int`, `depends_on: list[str]`.
* `Plan`: fields `goals: list[str]`, `assumptions: list[str]`, `info_needed: list[str]`, `tasks: list[Task]`, `acceptance_criteria: list[str]`, `stop_conditions: list[str]`.
* `EvidenceItem`: fields `id: str`, `kind: Literal['postgres','graph','web']`, `space: str`, `doc_id: str`, `locator: str`, `heading_path: list[str]`, `snippet: str`, `score: float`, `meta: dict`.
* `EvidencePack`: fields `budget_tokens: int`, `sources: list[EvidenceItem]`, `derived_notes: list[str]`, `missing_info: list[str]`, `redactions: list[str]`.
* Optionally add `Run` / `Event` models to log execution state and cost.

These models will enforce the plan and evidence formats that your Planner and Workflow Manager must return.

---

## 2. Implement an LLM client

**File:** `src/my_agentic_chatbot/llm_calls/llm_client.py`

Write a lightweight wrapper around LiteLLM / OpenAI HTTP endpoints.  It should:

1. Read model and API key from environment or the `ops/litellm/config.yaml` file.
2. Provide methods like `async def chat(model_name: str, messages: list[dict], max_tokens: int) -> str`.
3. Optionally add error/retry logic and respect per-model timeouts.

---

## 3. Implement an MCP client

**File:** `src/my_agentic_chatbot/mcp_client/mcp_client.py`

This module should handle synchronous calls to your MCP servers (`postgres`, `neo4j`, `web`).  It can expose a function:

```python
import httpx

async def call_tool_sync(server_url: str, tool_name: str, args: dict) -> dict:
    # Compose the POST request to the MCP server
    resp = await httpx.post(f"{server_url}/call-tool", json={"tool": tool_name, "args": args}, timeout=15)
    resp.raise_for_status()
    return resp.json()
```

Read server URLs and auth tokens from `ops/mcp/servers.yaml`.

---

## 4. Build the tool adapters

### 4.1 DB Tools

**File:** `src/my_agentic_chatbot/tools/db_tools.py`

Functions to query Postgres/ParadeDB via the MCP:

* `async def keyword_search(query: str, limit: int = 12) -> list[EvidenceItem]`: call `db_keyword` tool on the Postgres MCP server; it should return snippet + locator only.
* `async def vector_search(query: str, space: str, limit: int = 12) -> list[EvidenceItem]`: call `db_vector` tool with embeddings.
* Use reciprocal-rank fusion (RRF) to merge results and dedupe by `(doc_id, chunk_id)`.
* Map each row to `EvidenceItem` with `kind='postgres'`.

### 4.2 Graph Tools

**File:** `src/my_agentic_chatbot/tools/graph_tools.py`

* `async def graph_query(question: str) -> list[EvidenceItem]`: call `neo4j_query` on the Neo4j MCP server with strict timeouts (e.g., 8 s).
* The MCP should return truncated path summaries; convert them into `EvidenceItem` objects.

### 4.3 Web Tools (SearxNG)

**File:** `src/my_agentic_chatbot/tools/web_tools.py`

Implement the beefed-up search agent described earlier:

1. Create multiple query variants from the user’s question (e.g., synonyms, entity hints, date ranges).
2. For each variant, call a `web_search` tool on the Web MCP server (SearxNG).  Limit to ~5–10 results.
3. Filter results (drop ads/SEO pages), compute topicality and authority scores, deduplicate near-duplicates, and extract concise snippets.
4. Use RRF to combine ranking signals and return the top results as `EvidenceItem` objects with `kind='web'`.
5. Enrich results by extracting entities and upserting them into Neo4j if desired (see earlier planning).

---

## 5. Write the Planner

**File:** `src/my_agentic_chatbot/planner/main_planner.py`

Create a function `async def plan_from_message(message: str) -> Plan`:

1. Build a prompt that includes only the user’s message and a high-level description of available tools (without listing tool internals) to minimize token use.
2. Use the LLM client to call the `planner` model (e.g., via Claude or GPT-4).  Instruct the model to output a JSON object conforming to the `Plan` schema.
3. Validate the returned JSON against the `Plan` model; assign default budgets/timeouts if missing.

---

## 6. Construct the Prefect workflow

**File:** `src/my_agentic_chatbot/workflows/orchestrator.py`

Implement a Prefect flow `run_plan` with the following tasks:

1. **Call Planner** with the user message and get a `Plan`.
2. **Approval #1** – Pause until the user approves or edits the plan.
3. For each `Task` in the plan:

   * Dispatch to the appropriate tool function (`keyword_search`, `vector_search`, `graph_query`, `web_search`) based on the `Task.tool`.
   * Respect `Task.budget_tokens` and `Task.timeout_s`.
4. Collect all `EvidenceItem` objects into an `EvidencePack`:

   * Fuse results via RRF, dedupe, enforce the ≤4 k token cap and remove raw embeddings.
   * Create an `ExecutionReport` summarizing costs and run metadata.
5. **Approval #2** – Pause to review the evidence pack.
6. **Call Response Agent** via the LLM client.  Pass the user message and the `EvidencePack`.  The prompt should instruct the model to produce a final answer with citations (mapping claims to `EvidenceItem.id`), mention confidence and note unresolved items.
7. **Approval #3** – Final human review before returning the answer.
8. Log the run and costs.

Optionally define helper tasks like `pause_for_approval(prompt: str)` in `approver.py` and policies in `policies.py` to centralize budgets/timeouts and dedup/compaction rules.

---

## 7. Implement the Response Agent

**File:** `src/my_agentic_chatbot/response/responder.py`

Write a function `async def respond(user_message: str, evidence_pack: EvidencePack) -> str`:

1. Compose a system prompt that lists the evidence items (ID, snippet, source type) and reminds the model to cite sources and perform contradiction checks.
2. Use the LLM client (model name `responder`) to get the final answer.  Validate that citations refer only to IDs in the `EvidencePack`; if not, request a regeneration.

---

## 8. Prepare ETL and KB scripts

* **`ops/scripts/migrate.py`** – Applies `storage/models.sql` to create `kb_documents`, `kb_chunks`, `kb_embedding_space`, `kb_embeddings` as per the sidecar schema.
* **`ops/scripts/ingest_docs.py`** – Takes a file path or URL, extracts structural blocks (headings, paragraphs, tables, code), computes `tsv` columns, and inserts into `kb_documents` and `kb_chunks`.
* **`ops/scripts/embed_chunks.py`** – Loops over rows in `kb_documents` and `kb_chunks`, calls the embedding models (via LiteLLM) for general/code/law spaces, stores embeddings in `kb_embeddings`, and maintains HNSW indexes.

Write the SQL DDL in `src/my_agentic_chatbot/storage/models.sql` to match the table definitions and indexes described in the instructions.

---

## 9. Configure ops files

* **`ops/litellm/config.yaml`** – Use the sample config from the instructions: define your models (`planner`, `responder`, `cheap-worker`, `emb-general`, `emb-code`, `emb-law`), API keys, timeouts, retry policies and routing strategies.
* **`ops/mcp/servers.yaml`** – List the MCP endpoints and tokens for Postgres, Neo4j and Web servers as shown in the instructions.
* **`.env.example`** – Ensure environment variables for your API keys, database URLs and MCP tokens are documented.

---

## 10. Write tests

Under `src/my_agentic_chatbot/tests/`, create tests such as:

* `test_planner.py` – Assert that `plan_from_message()` returns a `Plan` with non-empty tasks and budgets/timeouts.
* `test_db_tools.py` – Mock the MCP DB server and verify that `keyword_search` and `vector_search` return no more than N rows and exclude raw embeddings.
* `test_orchestrator.py` – Use Prefect’s testing harness to run a dummy plan through the flow and ensure the `EvidencePack` respects size caps and approvals.
* `test_responder.py` – Check that citations refer to valid evidence IDs and that unsupported claims trigger warnings.

---

## 11. Run and iterate

1. Set up your environment (`docker compose up`, `make migrate`, `make run-proxy`, `make run-api`).
2. Ingest a small corpus with `python ops/scripts/ingest_docs.py` and embed with `python ops/scripts/embed_chunks.py`.
3. Hit your FastAPI endpoint (`POST /run`) from OpenWebUI to test the full pipeline.
4. Adjust ranking weights, timeouts and summarization logic as you see how the agent behaves.

---

### Summary

These steps map directly onto the missing pieces identified in `instructions.md` and incorporate the improvements we discussed (beefed-up SearxNG ranking, context discipline, retrieval fusion, subflow/state machine for the workflow agent).  They are deliberately modular so you can implement and test each component incrementally.
