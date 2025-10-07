CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS kb;

CREATE TABLE IF NOT EXISTS kb.ingest_items (
    id UUID PRIMARY KEY,
    job_id UUID,
    source_type TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    display_name TEXT NOT NULL,
    mime_type TEXT,
    language TEXT,
    domain TEXT,
    domain_confidence NUMERIC,
    status TEXT NOT NULL DEFAULT 'pending',
    document_summary TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,
    error_info JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kb_ingest_items_status ON kb.ingest_items (status);
CREATE INDEX IF NOT EXISTS idx_kb_ingest_items_domain ON kb.ingest_items (domain);

CREATE TABLE IF NOT EXISTS kb.documents (
    id UUID PRIMARY KEY,
    ingest_item_id UUID REFERENCES kb.ingest_items(id) ON DELETE CASCADE,
    title TEXT,
    text_full TEXT,
    tsv TSVECTOR,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kb_documents_ingest_item ON kb.documents (ingest_item_id);
CREATE INDEX IF NOT EXISTS idx_kb_documents_tsv ON kb.documents USING GIN (tsv);

CREATE TABLE IF NOT EXISTS kb.chunks (
    id UUID PRIMARY KEY,
    ingest_item_id UUID REFERENCES kb.ingest_items(id) ON DELETE CASCADE,
    document_id UUID REFERENCES kb.documents(id) ON DELETE SET NULL,
    chunk_index INTEGER NOT NULL,
    heading_path TEXT[] DEFAULT ARRAY[]::TEXT[],
    kind TEXT,
    text TEXT NOT NULL,
    summary TEXT,
    token_count INTEGER,
    overlap_tokens INTEGER,
    ner_entities JSONB DEFAULT '[]'::jsonb,
    tsv TSVECTOR,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (ingest_item_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_kb_chunks_document ON kb.chunks (document_id);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_tsv ON kb.chunks USING GIN (tsv);

CREATE TABLE IF NOT EXISTS kb.embedding_spaces (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    model TEXT NOT NULL,
    provider TEXT NOT NULL,
    dims INTEGER NOT NULL,
    distance_metric TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kb.chunk_embeddings (
    chunk_id UUID REFERENCES kb.chunks(id) ON DELETE CASCADE,
    space_id INTEGER REFERENCES kb.embedding_spaces(id) ON DELETE CASCADE,
    embedding VECTOR,
    score_meta JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (chunk_id, space_id)
);

CREATE TABLE IF NOT EXISTS kb.document_embeddings (
    document_id UUID REFERENCES kb.documents(id) ON DELETE CASCADE,
    space_id INTEGER REFERENCES kb.embedding_spaces(id) ON DELETE CASCADE,
    embedding VECTOR,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (document_id, space_id)
);

CREATE SCHEMA IF NOT EXISTS agent;

CREATE TABLE IF NOT EXISTS agent.runs (
    id UUID PRIMARY KEY,
    user_message TEXT NOT NULL,
    user_metadata JSONB DEFAULT '{}'::jsonb,
    planner_model TEXT,
    responder_model TEXT,
    audit_model TEXT,
    plan JSONB,
    response JSONB,
    audit_report JSONB,
    evidence JSONB,
    success BOOLEAN,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    duration_ms INTEGER,
    chat_ingest_item_id UUID,
    metadata JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_started_at ON agent.runs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_success ON agent.runs (success);

CREATE TABLE IF NOT EXISTS agent.events (
    id BIGSERIAL PRIMARY KEY,
    run_id UUID REFERENCES agent.runs(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    task_id TEXT,
    tool TEXT,
    status TEXT,
    payload JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_events_run ON agent.events (run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_events_type ON agent.events (event_type);

CREATE TABLE IF NOT EXISTS agent.web_work_items (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL,
    requirement_id TEXT,
    task_id TEXT,
    query TEXT NOT NULL,
    source_url TEXT,
    source_title TEXT,
    raw_result JSONB DEFAULT '{}'::jsonb,
    snippet TEXT,
    query_embedding VECTOR(768),
    snippet_embedding VECTOR(768),
    retrieval_score NUMERIC,
    fetch_status TEXT,
    http_status INTEGER,
    fetched_at TIMESTAMPTZ,
    rendered_at TIMESTAMPTZ,
    html TEXT,
    markdown TEXT,
    summary TEXT,
    authority_score NUMERIC,
    topicality_score NUMERIC,
    locality_score NUMERIC,
    curated BOOLEAN DEFAULT FALSE,
    curated_reason TEXT,
    kb_document_id UUID,
    kb_chunk_ids UUID[] DEFAULT ARRAY[]::UUID[],
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ DEFAULT NOW() + INTERVAL '48 hours'
);

CREATE INDEX IF NOT EXISTS idx_web_work_items_run ON agent.web_work_items (run_id);
CREATE INDEX IF NOT EXISTS idx_web_work_items_curated ON agent.web_work_items (run_id, curated);
CREATE INDEX IF NOT EXISTS idx_web_work_items_expires ON agent.web_work_items (expires_at);
