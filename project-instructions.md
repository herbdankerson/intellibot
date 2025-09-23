# instructions.md

> **Project**: High-Fidelity Agentic Chatbot (Planner → Workflow Manager → Tool Agents via MCP → Response Agent)
> **Stack**: OpenWebUI (chat UI), FastAPI, LiteLLM Proxy, Prefect, FastMCP, Postgres + pgvector (+ ParadeDB optional), Neo4j (optional), Sidecar KB schema, MCP servers
> **Goal**: Deep-thinking personal assistant that decomposes large tasks into micro-tasks, retrieves just-enough evidence with strict context budgets, uses human-in-the-loop approvals, and produces sourced, audited answers.

---

## 0) Executive Overview

* **Why agentic**: Naïve RAG breaks on multi-step, multi-source questions. We adopt **Modular/Agentic RAG**: a **Planner LLM** composes a plan; a **Workflow Manager** orchestrates Tool Agents; **MCP** is the standardized “tool bus”; a **Response Agent** audits and answers.
* **Context strategy**: Start each user turn with **only the user message**. The Planner pulls **minimal** DB context via Tool Agents (Postgres/ParadeDB + pgvector; Neo4j only for multi-hop). Collected results are curated into a **strict EvidencePack** (compact, deduped, token-capped) for the final LLM.
* **Ops simplification**: Use **LiteLLM Proxy** to unify all models/embeddings behind OpenAI-compatible endpoints. Use **Prefect** for orchestration + **HITL approvals**. Use **FastMCP** to talk to prebuilt MCP servers (Postgres, Neo4j, Web).
* **OpenWebUI (OWUI)**: Keep as the chat UI. For knowledge, use a **sidecar KB schema** (doc-level vectors + intelligent structural chunks + multiple embedding spaces). Optionally bridge OWUI via views or automate ingestion via OWUI’s APIs.

---

## 1) Repository & Runtime Layout

```
my_agentic_chatbot/
├─ .env.example
├─ pyproject.toml
├─ requirements.txt
├─ README.md
├─ docker-compose.yml              # dev: postgres, litellm, neo4j (opt), prefect, mcp
├─ Makefile
│
├─ ops/
│  ├─ litellm/config.yaml          # models, embeddings, routing, keys, limits
│  ├─ mcp/servers.yaml             # MCP endpoints & auth (pg, neo4j, web)
│  ├─ prefect/deployment.yaml      # (optional) Prefect deployment
│  └─ scripts/
│     ├─ migrate.py                # applies storage/models.sql
│     ├─ ingest_docs.py            # ETL: load files/urls into kb_*
│     └─ embed_chunks.py           # ETL: embed doc+chunks for spaces
│
├─ src/my_agentic_chatbot/
│  ├─ main.py                      # FastAPI: /run, /health; (optional) OpenAI pass-thru
│  ├─ config.py                    # read .env + yaml; central constants
│  ├─ logging_conf.py              # structured logging
│  ├─ schemas.py                   # Plan, Task, EvidencePack, EvidenceItem, Run, Event
│  │
│  ├─ llm_calls/
│  │  └─ llm_client.py             # tiny client (LiteLLM SDK or Proxy)
│  │
│  ├─ mcp_client/
│  │  └─ mcp_client.py             # FastMCP wrapper call_tool_sync(url, name, args)
│  │
│  ├─ tools/                       # thin adapters over MCP servers
│  │  ├─ db_tools.py               # BM25/pgvector via Postgres MCP
│  │  ├─ graph_tools.py            # NL→Cypher via Neo4j MCP (strict limits)
│  │  └─ web_tools.py              # (optional) web/search MCP
│  │
│  ├─ planner/
│  │  ├─ prompts/
│  │  │  ├─ planner_system.md      # no context except user msg; budgets; tool minimality
│  │  │  └─ rubrics.md             # graph_needed?, minimality, budget sanity
│  │  └─ main_planner.py           # plan_from_message(model, msg) -> Plan (JSON)
│  │
│  ├─ workflows/
│  │  ├─ orchestrator.py           # Prefect Flow: plan → approvals → tasks → evidence
│  │  ├─ policies.py               # budgets, timeouts, truncation, dedupe, redaction
│  │  └─ approver.py               # reusable HITL pause helpers
│  │
│  ├─ response/
│  │  ├─ prompts/responder_system.md
│  │  └─ responder.py              # respond(model, msg, EvidencePack) -> str
│  │
│  ├─ storage/
│  │  ├─ models.sql                # sidecar KB schema (docs/chunks/spaces/embeddings)
│  │  ├─ db.py                     # connection helpers
│  │  └─ events.py                 # run/event/cost logging
│  │
│  └─ util/
│     ├─ text.py                   # snippetting, reciprocal-rank fusion, dedupe
│     ├─ timing.py                 # timeouts/retries/backoff
│     └─ tracing.py                # request/run IDs
│
└─ tests/
   ├─ test_planner.py
   ├─ test_orchestrator.py
   ├─ test_db_tools.py
   └─ test_responder.py
```

---

## 2) Core Architecture & Contracts

### Roles

* **Planner (LLM)**
  Input: user message only.
  Output: `Plan { goals[], assumptions[], info_needed[], tasks[], stop_conditions[], acceptance_criteria[] }` (JSON).
  Responsibilities: Decompose into **minimal** tasks, assign **budgets/timeouts**, query DB first, propose extra research if needed, gate Neo4j to true multi-hop.

* **Workflow Manager (Prefect)**
  Input: `Plan`. Output: executed DAG → `EvidencePack` + `ExecutionReport`.
  Responsibilities: map tasks to Tool Agents (MCP), enforce budgets/timeouts, **HITL approvals** at (Plan, Workflow, Final Answer), assemble evidence.

* **Tool Agents (MCP servers)**

  * **DB Manager**: Postgres/ParadeDB hybrid; pgvector semantic; returns compact rows + locators/snippets.
  * **Graph Reasoner**: Neo4j NL→Cypher; strict timeouts + truncation.
  * (Optional) Web / Files / Git MCP servers.

* **Response Agent (LLM)**
  Input: user message + `EvidencePack` + `ExecutionReport`.
  Output: final answer + citations + confidence + unresolved items.
  Responsibilities: self-checks (claim→source mapping, contradiction detection, acceptance criteria).

### Contracts (Pydantic)

* `Task { id, intent, tool('db_keyword'|'db_vector'|'graph'|'web'), inputs, budget_tokens, timeout_s, depends_on[] }`
* `Plan { goals[], assumptions[], info_needed[], tasks[], acceptance_criteria[], stop_conditions[] }`
* `EvidenceItem { id, kind(postgres|graph|web), space, doc_id, locator, heading_path[], snippet, score, meta{} }`
* `EvidencePack { budget_tokens, sources[EvidenceItem], derived_notes[], missing_info[], redactions[] }`

### Context Engineering Rules

* Cold start → **user message only**.
* Planner **asks** tools for minimal slices; avoid flooding context.
* EvidencePack hard cap (e.g., **≤ 4k tokens**).
* Strict per-tool **timeouts** (8–15s) + **row caps** + field allowlists.
* Remove embeddings/large blobs from tool outputs; only snippets + locators.

---

## 3) Knowledge Base (Sidecar Schema)

> Keep OWUI as chat UI; use this **sidecar** for advanced RAG. Optionally expose read-only views to make it look like OWUI’s tables.

**Tables**

* `kb_documents(id, external_id, source, uri, title, mime_type, lang, metadata, text_full, tsv)`
* `kb_chunks(id, doc_id, chunk_idx, kind, heading_path[], text, char_start, char_end, metadata, tsv)`
* `kb_embedding_space(id, name('general'|'code'|'law'...), provider, model, dims, distance)`
* `kb_embeddings(id, space_id, doc_id, chunk_id, level('document'|'chunk'), embedding)`

**Indexes**

* `GIN` on `kb_documents.tsv`, `kb_chunks.tsv`
* `HNSW` per `(space_id, level)` on `kb_embeddings.embedding` (vector\_cosine\_ops or as needed)

**Intelligent chunking**

* Parse **structural blocks**: headings/sections/paragraphs/tables/code; store `kind`, `heading_path`, offsets.
* Maintain **both** doc-level vectors (broad “gist”) and chunk-level vectors (precision).
* Maintain **domain spaces** (e.g., general, code, law) for both levels.

---

## 4) Retrieval Policy

1. **Keyword coverage**: BM25/ts\_rank on `kb_chunks.tsv` (and optionally `kb_documents.tsv`).
2. **Vectors**:

   * General/doc-level (coarse recall)
   * General/chunk-level (precision)
   * Domain/chunk-level (law/code) **only when** classifier/rules flag it
3. **Fuse** with **Reciprocal Rank Fusion (RRF)**; dedupe by `(doc_id, chunk_id)`; cap to N (\~12).
4. Snippets: 2–3 sentences; compact preview for tables unless asked for the full.

---

## 5) Human-in-the-Loop (HITL)

* **Approval #1**: Plan (scope, budgets, tools).
* **Approval #2**: Evidence (sources, sufficiency, cost).
* **Approval #3**: Final answer (meets acceptance criteria).
  All three are implemented as **Prefect** pauses with small typed prompts.

---

## 6) Security & Limits

* Start **read-only** Postgres MCP; elevate only with allowlists + approvals.
* Timeouts, row caps, **parameterized** queries.
* Token budgets: Planner(≤2k), EvidencePack(≤4k), Response uses remainder.
* Observability: log all runs/events/costs in Postgres.

---

## 7) OpenWebUI Integration

**Use-cases**

* **Simplest**: Use OWUI for chat only (point to LiteLLM Proxy). Retrieval in your backend via MCP over sidecar `kb_*` tables.
* **Semi-integrated**: Create SQL **views** to mirror OWUI KB shapes; let OWUI “see” enhanced chunks read-only.
* **Automated OWUI ingestion**: Use OWUI management APIs from Prefect; set embeddings endpoint to **LiteLLM Proxy**, expose **virtual embedding models** (`emb-general`, `emb-code`, `emb-law`) and batch ingest per domain by flipping the selected model.

**Embedding strategy**

* **General**: Google `text-embedding-004` (or equivalent).
* **Specialized**: Code/Law encoders as additional spaces.
* Maintain separate HNSW indexes per space + level.

---

## 8) LiteLLM & Key Management

* **Proxy** exposes OpenAI-compatible `/v1/chat/completions` and `/v1/embeddings`.
* **Multiple keys** per provider supported; **round-robin** or **failover** on 429/quota.
* **Virtual models**: map `planner`, `responder`, `cheap-worker`; map `emb-general`, `emb-code`, `emb-law`.
* Route per request (explicit `model`) or via **policy** (virtual model names).

**Sample `ops/litellm/config.yaml`**

```yaml
litellm_settings:
  key_management: round_robin
  timeout: 60
  max_retries: 2
  retry_backoff: 2

routing_strategy: failover

model_list:
  # Chat roles (virtual)
  - model_name: planner
    litellm_params: { model: claude-3-5-sonnet, api_key: ${ANTHROPIC_KEY} }
  - model_name: planner
    litellm_params: { model: gpt-4o, api_key: ${OPENAI_KEY_1} }

  - model_name: responder
    litellm_params: { model: gpt-4o, api_key: ${OPENAI_KEY_2} }

  - model_name: cheap-worker
    litellm_params: { model: gpt-4o-mini, api_key: ${OPENAI_KEY_3} }

  # Embedding spaces
  - model_name: emb-general
    litellm_params: { model: text-embedding-004, api_key: ${GOOGLE_API_KEY} }
  - model_name: emb-code
    litellm_params: { model: text-embedding-3-large, api_key: ${OPENAI_KEY_1} }
  - model_name: emb-law
    litellm_params: { model: cohere/embed-english-v3.0, api_key: ${COHERE_KEY} }
```

---

## 9) MCP Servers

**`ops/mcp/servers.yaml`**

```yaml
postgres:
  transport: http
  url: http://localhost:8081/api/mcp/
  auth: { bearer: ${POSTGRES_MCP_TOKEN} }

neo4j:
  transport: http
  url: http://localhost:8082/api/mcp/
  auth: { bearer: ${NEO4J_MCP_TOKEN} }

web:
  transport: http
  url: http://localhost:8083/api/mcp/
```

Use **prebuilt Postgres** (start read-only) and **neo4j-contrib** servers. Add others (web/files/git) as needed.

---

## 10) API & Orchestration

**FastAPI (`src/my_agentic_chatbot/main.py`)**

* `POST /run` → kicks the Prefect flow (Planner → Approvals → Tasks → Evidence → (optional) Respond).
* `GET /healthz` → readiness probe.
* (Optional) `/v1/chat/completions` passthrough to LiteLLM if you want a single host.

**Prefect Flow (`src/my_agentic_chatbot/workflows/orchestrator.py`)**

1. Call Planner (model=`planner`) with **only** the user message.
2. **Approval #1**: approve Plan.
3. For each `Task`, call the right Tool Agent (MCP) with **timeouts/row caps**.
4. Curate **EvidencePack** (rank-fuse, dedupe, truncate, redact).
5. **Approval #2**: approve Evidence.
6. Response Agent (model=`responder`) → final answer or requests micro-tasks.
7. **Approval #3**: release answer.
8. Log run + costs.

---

## 11) ETL Pipelines (Prefect Jobs)

* `ops/scripts/ingest_docs.py`

  * Fetch URI → parse structural blocks (headings/sections/paras/tables/code).
  * Insert `kb_documents`, `kb_chunks`, compute `tsv` for both.

* `ops/scripts/embed_chunks.py`

  * For each **space** (`emb-general`, `emb-code`, `emb-law`), embed **document-level** + **chunk-level** as needed.
  * Insert into `kb_embeddings`; maintain HNSW indexes per `(space_id, level)`.
  * Keep encoder versioning in `kb_embedding_space.model`; don’t overwrite old rows if you re-embed—use a `current` flag or validity period.

---

## 12) Makefile Targets

```
run-proxy:    ## start LiteLLM proxy
	litellm --config ops/litellm/config.yaml --port 4000

run-api:      ## start FastAPI app
	uvicorn src.my_agentic_chatbot.main:app --reload --port 8000

run-prefect:
	prefect server start

migrate:
	python ops/scripts/migrate.py

ingest:
	python ops/scripts/ingest_docs.py

embed:
	python ops/scripts/embed_chunks.py
```

---

## 13) Tests (Minimum)

* `test_planner.py`: planner emits valid `Plan`; tasks have budgets/timeouts.
* `test_db_tools.py`: DB MCP returns ≤N rows; snippets ≤max chars; no embeddings in payload.
* `test_orchestrator.py`: happy path plan→evidence with approvals; EvidencePack size cap enforced.
* `test_responder.py`: citations reference `EvidenceItem.id`; flags unsupported claims.

---

## 14) Recommended Codex Platform (Web vs CLI)

> “Codex” here refers to your coding agent environment with recent updates. Use **both** for the best DX:

* **Codex Web** (recommended for planning & review)

  * Strong at code review, refactors, multi-file edits, PR guidance.
  * Ideal for iterating on prompts (Planner/Responder system files), YAML (LiteLLM/MCP), and SQL schemas with quick visual diff.

* **Codex CLI** (recommended for execution & automation)

  * Drive **scaffold generation**, repetitive edits across the tree, and **scripted tasks** (e.g., inject stubs, update imports, bump configs).
  * Great for “apply patch” workflows, templating, and regenerating files from specs (like this `instructions.md`).

**Suggested workflow**

1. Use **Codex Web** to review/modify the structure (files above), fill in TODOs, refine prompts and YAML.
2. Use **Codex CLI** to **generate stubs** for all modules, wire imports, and produce runnable **starter code** (FastAPI app, Prefect flow skeletons, ETL scripts, SQL DDL).
3. Iterate: run `docker compose up`, `make migrate`, `make run-proxy`, `make run-api`, and test `/run`.

---

## 15) Milestones (do these in order)

1. LiteLLM Proxy running; OpenWebUI connects as OpenAI-compatible; models visible (`planner`, `responder`, `cheap-worker`, `emb-*`).
2. Sidecar KB schema applied; `migrate.py` OK.
3. Planner returns valid `Plan` JSON; unit test passes.
4. DB MCP (read-only) wired; BM25 query round-trips.
5. Prefect flow executes plan → tasks → EvidencePack; approvals working.
6. ETL: ingest → intelligent chunks → general doc+chunk embeddings.
7. Retrieval: hybrid (BM25 + vectors), RRF fusion, tight snippets.
8. Response Agent: citations, contradiction checks, acceptance criteria gating.
9. Domain spaces (code/law) + rule/LLM classifier gating.
10. Neo4j MCP path for multi-hop queries (guarded; optional).

---

## 16) Environment Variables (`.env.example`)

```
OPENAI_KEY_1=
OPENAI_KEY_2=
ANTHROPIC_KEY=
GOOGLE_API_KEY=
COHERE_KEY=

DATABASE_URL=postgresql://user:pass@localhost:5432/agentdb

POSTGRES_MCP_TOKEN=
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=
NEO4J_MCP_TOKEN=
```

---

## 17) Non-Negotiables (to keep quality high)

* **No raw dumps** into the LLM. Always EvidencePack with budgets.
* **BM25 first, vectors second**, domain vectors gated by rubric.
* **Graph only when multi-hop is truly needed**.
* **Approvals** at Plan / Workflow / Final.
* **Read-only** DB MCP until strictly required otherwise.
* **Observability**: log runs, events, costs.

---

### You can hand this file to Codex Web/CLI to bootstrap the repo.

If you want, I can produce a minimal **starter set of files** (empty modules + stub functions + config templates) matching this layout so you can `docker compose up`, `make migrate`, and hit `/run` immediately.
