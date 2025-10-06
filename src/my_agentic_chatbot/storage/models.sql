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

