CREATE TABLE IF NOT EXISTS kb_documents (
    id SERIAL PRIMARY KEY,
    external_id TEXT UNIQUE,
    title TEXT NOT NULL,
    uri TEXT,
    tsv tsvector,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS kb_chunks (
    id SERIAL PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    heading TEXT,
    content TEXT NOT NULL,
    tsv tsvector,
    UNIQUE (document_id, ordinal)
);

CREATE TABLE IF NOT EXISTS kb_embedding_spaces (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    model TEXT NOT NULL,
    is_current BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS kb_embeddings (
    id SERIAL PRIMARY KEY,
    chunk_id INTEGER REFERENCES kb_chunks(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES kb_documents(id) ON DELETE CASCADE,
    space_id INTEGER NOT NULL REFERENCES kb_embedding_spaces(id),
    level TEXT NOT NULL CHECK (level IN ('document', 'chunk')),
    embedding VECTOR(1536),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kb_documents_tsv ON kb_documents USING GIN (tsv);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_tsv ON kb_chunks USING GIN (tsv);
