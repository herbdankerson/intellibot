# 0002 – Ingestion Versioning & Mandatory BM25

## Context

The original ETL stored documents, chunks, and embeddings without version tracking. Re-ingesting the same source produced duplicate rows and reissued UUIDs, making it difficult to audit changes or resume partial runs. Search used `ts_rank_cd` as a fallback when BM25 functions were unavailable.

## Decision

- Add `version`, `content_digest`, `updated_at`, and filename metadata across `kb.documents`, `kb.chunks`, `kb.chunk_embeddings`, `kb.document_embeddings`, and `kb.entries`.
- Rework `persist_results` to detect existing documents via `source_uri`, reuse matching chunk hashes, and only enqueue TEI calls for new/changed content.
- Copy forward embeddings and summaries for chunks that remain identical between versions while bumping version numbers when the document digest changes.
- Require the ParadeDB `pg_search` extension and fail loudly if `bm25(tsvector, tsquery)` is missing. `kb.search_entries` now always uses BM25 for text ranking and raises an exception when the function is unavailable.

## Consequences

- Re-ingesting a document is idempotent: unchanged chunks/embeddings are reused, and new versions clearly identify deltas.
- Partial runs can resume without bloating the knowledge base or wasting embedding quota.
- Hybrid search quality improves and remains predictable because BM25 is always available; misconfigured environments surface immediately during migrations.
- Operational docs now include a runbook (`project-docs/ingestion-flow.md`) to validate row counts and BM25 availability after each ingest.
