-- Create per-space HNSW indexes for chunk and document embedding tables.
-- Resolves embedding spaces dynamically using cfg.active -> kb.embedding_spaces.
DO $$
DECLARE
    rec RECORD;
    sanitized TEXT;
BEGIN
    FOR rec IN
        SELECT s.id, s.name
        FROM kb.embedding_spaces AS s
        JOIN cfg.active AS a ON a.value = s.name
        WHERE a.key IN ('active_emb_general', 'active_emb_legal', 'active_emb_code')
    LOOP
        sanitized := regexp_replace(lower(rec.name), '[^a-z0-9]+', '_', 'g');
        IF sanitized IS NULL OR length(sanitized) = 0 THEN
            sanitized := 'space_' || rec.id::text;
        END IF;

        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON kb.chunk_embeddings USING hnsw (embedding vector_cosine_ops) '
            || 'WITH (m = 16, ef_construction = 200) WHERE space_id = %s',
            'idx_chunk_embeddings_' || sanitized,
            rec.id
        );

        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON kb.document_embeddings USING hnsw (embedding vector_cosine_ops) '
            || 'WITH (m = 16, ef_construction = 200) WHERE space_id = %s',
            'idx_document_embeddings_' || sanitized,
            rec.id
        );
    END LOOP;
END $$;
