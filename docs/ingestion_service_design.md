# Ingestion Service & Knowledge Base Design

This document specifies the ETL and orchestration architecture for the
knowledge-base intake pipeline that will power document and website
ingestion for the agentic chatbot.

## 1. Goals & Requirements

- Accept uploads from Open WebUI and direct API clients, plus URL batches
  emitted by search agents.
- Normalize each artifact (document or webpage) with Docling, keeping the
  same chunking and overlap rules used by the Legal Knowledge MCP server.
- Classify each artifact with Gemini 2.5 Flash to label the domain (`legal`,
  `code`, `general`) and record the acquired source type (`document`,
  `website`, `search-result`, etc.).
- Run Named-Entity Recognition (NER) over each chunk using the same
  enrichment logic as the legal MCP server so entity metadata is stored for
  downstream filtering.
- Generate embeddings with Gemini’s `models/embedding-001` for general
  content, Voyage `voyage-law-2` for legal content, and Voyage
  `voyage-code-3` for code-related content. (Legal space still uses
  `voyage-law-2`; the update only affects the code space.)
- Persist the enriched chunks and metadata in shared `kb` tables so all
  knowledge-backed features use the same vector index.
- Mirror successful document ingests into the Open WebUI `documents` table
  so uploads appear in the UI library, while keeping website ingests in the
  sidecar catalog.

## 2. Data Model (`kb` schema)

All ingestion artifacts share a common set of metadata columns and then fan
out into chunk/embedding records.

### 2.1 `kb.ingest_items`

| column               | type              | description |
|----------------------|-------------------|-------------|
| `id`                 | UUID (PK)         | Internal identifier for the ingest job item. |
| `job_id`             | UUID              | Prefect flow run ID for traceability. |
| `source_type`        | text              | `document`, `website`, `search-result`, etc. |
| `source_uri`         | text              | Original file path, upload name, or URL. |
| `display_name`       | text              | Human-friendly name (e.g., filename without extension). |
| `mime_type`          | text              | Detected MIME / file type. |
| `language`           | text              | ISO 639 language code detected by Docling. |
| `domain`             | text              | Domain classification (`legal`, `code`, `general`). |
| `domain_confidence`  | numeric           | Confidence score from Gemini classifier. |
| `status`             | text              | `pending`, `processing`, `succeeded`, `failed`. |
| `error_info`         | jsonb             | Error payload on failure. |
| `ingested_at`        | timestamptz       | Completion timestamp. |
| `metadata`           | jsonb             | Additional attributes (Docling diagnostics, crawl depth, etc.). |

### 2.2 `kb.documents`

Logical document records (only created for `source_type = 'document'` but
referenced by chunks regardless of origin).

| column          | type        | description |
|-----------------|-------------|-------------|
| `id`            | UUID (PK)   | Document identifier shared with Open WebUI mirror. |
| `ingest_item_id`| UUID (FK)   | Points to `kb.ingest_items`. |
| `title`         | text        | Display title, often derived from Docling metadata. |
| `text_full`     | text        | Optional full normalized plain text. |
| `tsv`           | tsvector    | Full-text index built from Docling text. |
| `metadata`      | jsonb       | Table of contents, Docling annotations, etc. |

### 2.3 `kb.chunks`

| column            | type        | description |
|-------------------|-------------|-------------|
| `id`              | UUID (PK)   | Chunk identifier (matches MCP chunk IDs for reuse). |
| `ingest_item_id`  | UUID (FK)   | Source ingest item. |
| `document_id`     | UUID (FK)   | Optional; populated for document uploads. |
| `chunk_index`     | integer     | Sequential index per source. |
| `heading_path`    | text[]      | Hierarchy of headings leading to the chunk. |
| `kind`            | text        | Structural type (`paragraph`, `table`, `code`, etc.). |
| `text`            | text        | Chunk text. |
| `token_count`     | integer     | Rough token estimate from chunker. |
| `overlap_tokens`  | integer     | Overlap parameter applied during chunking. |
| `ner_entities`    | jsonb       | Extracted entities in the legal MCP schema format. |
| `tsv`             | tsvector    | Keyword-search index. |
| `metadata`        | jsonb       | Additional chunk-level info (page numbers, URL fragment, etc.). |

### 2.4 `kb.embedding_spaces`

| column    | type      | description |
|-----------|-----------|-------------|
| `id`      | serial PK | Internal ID for embedding spaces. |
| `name`    | text      | `general`, `legal`, `code`. |
| `model`   | text      | `models/embedding-001`, `voyage-law-2`, `voyage-code-3`. |
| `provider`| text      | `google-gemini`, `voyage`. |
| `dims`    | integer   | Vector dimensionality. |
| `distance_metric` | text | `cosine` or `dot`. |

### 2.5 `kb.chunk_embeddings`

| column         | type        | description |
|----------------|-------------|-------------|
| `chunk_id`     | UUID (FK)   | Points to `kb.chunks`. |
| `space_id`     | integer (FK)| Embedding space used. |
| `embedding`    | vector      | pgvector column. |
| `score_meta`   | jsonb       | Metadata (prompt version, batching hints). |
| `created_at`   | timestamptz | Insertion timestamp. |

Indexes:
- `kb.chunks` → GIN on `tsv`, B-Tree on `(ingest_item_id, chunk_index)`.
- `kb.chunk_embeddings` → HNSW index per `space_id` using cosine distance.

## 3. Prefect Ingestion Flow (`document_intake_flow`)

### 3.1 High-Level Steps

1. **Register Item** — Create `kb.ingest_items` row with status `pending`,
   record source metadata (source type, filename/URL, MIME hints).
2. **Acquire Source** —
   - `document`: persist raw file in object storage; store path in metadata.
   - `website/search-result`: fetch HTML with requests + Trafilatura; include
     final URL and HTTP metadata.
3. **Docling Normalize** — Run Docling conversion to Markdown + structural
   JSON. Record detected language and any conversion warnings in metadata.
4. **Classify Domain** — Prompt Gemini 2.5 Flash with the normalized summary
   to output `{domain, confidence, rationale}`. Update ingest item status and
   metadata. If below confidence threshold, include fallback rationale and
   set `domain = 'general'` with a manual-review flag.
5. **Document Summary** — Use Gemini 2.5 Flash to generate a concise
   document-level synopsis plus key takeaways; persist both summary text and
   model metadata on the ingest item for reuse during retrieval and UI
   previews.
6. **Chunk & NER** — Apply `build_chunks` from
   `mcp/legal-knowledge-base/src/chunker.py`. For each chunk run the existing
   NER routine (same tag schema as the MCP server) and attach it to the
   chunk payload.
7. **Chunk Summaries** — Call Gemini 2.5 Flash to produce short (≤3 sentence)
   abstracts for each chunk. Store the summary alongside the chunk so agents
   can reference them when context budgets are tight.
8. **Budgeted Abstractions** — When a document produces high token counts,
   call Gemini 2.5 Flash summarizer to create higher-level abstraction
   chunks used when EvidencePack budgets overflow.
9. **Embed** — Batch chunk texts by domain:
   - `general` → Gemini `models/embedding-001`
   - `legal` → Voyage `voyage-law-2`
   - `code` → Voyage `voyage-code-3`
10. **Persist** — Insert/update:
   - `kb.documents` (documents only)
   - `kb.chunks`
   - `kb.chunk_embeddings`
   - `kb.ingest_items.status = 'succeeded'`
   Maintain transactions so partial failures mark the job as failed and
   capture errors in `error_info`.
11. **Mirror to Open WebUI** — For `source_type = 'document'`, upsert a row in
   OWUI’s `documents` table with status, size, and ingest timestamp so the UI
   shows the file. Website ingests are omitted from this mirror.

### 3.2 Prefect Task Modules

| task name                | implementation notes |
|--------------------------|----------------------|
| `register_ingest_item`   | sql task in `etl/tasks/ingest_tasks.py`. |
| `acquire_source`         | Handles file storage (S3/local) or HTTP fetch with per-domain timeouts. |
| `docling_normalize`      | Wrap Docling CLI/SDK; enforce size limits. |
| `classify_domain`        | Uses Gemini 2.5 Flash; returns domain + confidence + source type confirmation. |
| `chunk_and_ner`          | Imports `build_chunks` + NER helper from MCP repo. |
| `summarize_chunks`       | Optional summarizer when chunk count > policy. |
| `embed_chunks`           | Batches by embedding space; handles API retries. |
| `persist_results`        | Inserts into `kb` tables using connection pool. |
| `mirror_openwebui`       | Peewee/SQLAlchemy call into OWUI DB. |
| `finalize_status`        | Marks ingest item as succeeded/failed. |

All tasks log to Prefect, capturing timing, tokenizer budgets, and API costs.

## 4. API Contract

### 4.1 Upload Endpoint (`POST /api/v1/documents/upload`)
- Multipart form (`file`, `title?`).
- Response: `{ job_id, ingest_item_id, status_url }`.
- Internal flow: store file → enqueue Prefect flow run (`document_intake_flow`).

### 4.2 URL Intake (`POST /api/v1/documents/url`)
- Body: `{ urls: [...], labels?, crawl_depth? }`.
- For each URL create ingest item with `source_type = "website"`. Deduplicate
  against recent successes.

### 4.3 Job Status (`GET /api/v1/documents/jobs/{job_id}`)
- Returns Prefect state, ingest item status, processed chunk counts.

### 4.4 OWUI Compatibility
- Our service mimics OWUI’s existing JSON envelope (`{ status, progress,
  message }`).
- Configure OWUI’s RAG backend to use this API base URL.
- When an ingest succeeds and `source_type = 'document'`, insert into OWUI’s
  `documents` table with columns: `id`, `name`, `status`, `size`, `source`.

## 5. NER & Metadata Handling

- Reuse the entity extractor from the legal MCP server (port the module into
  `etl/tasks/ner_tasks.py`).
- Store raw entity spans + normalized labels in `kb.chunks.ner_entities`.
- Include detected entities in Prefect logs for auditability.

## 6. Settings & Policies

- Timeouts: acquisition 15 s, Docling 60 s, Gemini/Voyage calls 20 s.
- Batch sizes: embeddings batch of 8 for Gemini, 16 for Voyage (per token
  limits).
- Retry policy: exponential backoff (1s, 2s, 4s) with max 3 attempts for
  external APIs.
- Evidence budget: if total chunk token count > 4000, trigger summarization
  to maintain EvidencePack limits.

This design now reflects the updated model choices (`models/embedding-001`
for general, `voyage-code-3` for code), captures source types and filenames,
keeps NER in the loop, and aligns Docling chunking with the legal MCP server.
