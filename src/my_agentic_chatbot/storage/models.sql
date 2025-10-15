CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_search;

-- Legacy schemas are no longer used; drop them so schema drift does not persist.
DROP SCHEMA IF EXISTS project_code CASCADE;
DROP SCHEMA IF EXISTS project_docs CASCADE;

CREATE SCHEMA IF NOT EXISTS kb;
CREATE SCHEMA IF NOT EXISTS task;

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
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    is_dev BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_kb_ingest_items_status ON kb.ingest_items (status);
CREATE INDEX IF NOT EXISTS idx_kb_ingest_items_domain ON kb.ingest_items (domain);

CREATE TABLE IF NOT EXISTS kb.documents (
    id UUID PRIMARY KEY,
    ingest_item_id UUID REFERENCES kb.ingest_items(id) ON DELETE CASCADE,
    source_uri TEXT,
    file_name TEXT,
    title TEXT,
    text_full TEXT,
    tsv TSVECTOR,
    metadata JSONB DEFAULT '{}'::jsonb,
    summary TEXT,
    content_digest TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    is_dev BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_kb_documents_ingest_item ON kb.documents (ingest_item_id);
CREATE INDEX IF NOT EXISTS idx_kb_documents_tsv ON kb.documents USING GIN (tsv);
CREATE INDEX IF NOT EXISTS idx_kb_documents_source_uri ON kb.documents (source_uri);
CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_documents_source_version ON kb.documents (source_uri, version);

CREATE TABLE IF NOT EXISTS kb.entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID,
    ingest_item_id UUID REFERENCES kb.ingest_items(id) ON DELETE SET NULL,
    document_id UUID REFERENCES kb.documents(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source TEXT NOT NULL,
    uri TEXT,
    title TEXT,
    author TEXT,
    content TEXT NOT NULL,
    summary TEXT,
    meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    ner JSONB,
    emotions JSONB,
    embedding VECTOR(1024),
    is_document BOOLEAN NOT NULL DEFAULT FALSE,
    is_note BOOLEAN NOT NULL DEFAULT FALSE,
    needs_metadata BOOLEAN NOT NULL DEFAULT FALSE,
    is_chat BOOLEAN NOT NULL DEFAULT FALSE,
    version INTEGER NOT NULL DEFAULT 1,
    file_name TEXT,
    is_dev BOOLEAN NOT NULL DEFAULT FALSE,
    search_tsv TSVECTOR GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(content, '')), 'B')
    ) STORED
);

CREATE INDEX IF NOT EXISTS idx_kb_entries_ingest ON kb.entries (ingest_item_id);
CREATE INDEX IF NOT EXISTS idx_kb_entries_updated ON kb.entries (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_kb_entries_tsv ON kb.entries USING GIN (search_tsv);
CREATE INDEX IF NOT EXISTS idx_kb_entries_embedding_hnsw
    ON kb.entries USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

DROP INDEX IF EXISTS idx_kb_entries_bm25;
DROP INDEX IF EXISTS kb.idx_kb_entries_bm25;
CREATE INDEX IF NOT EXISTS idx_kb_entries_bm25
    ON kb.entries USING bm25 (
        id,
        title,
        summary,
        content,
        meta,
        emotions,
        ner
    )
    WITH (key_field = 'id');

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
    version INTEGER NOT NULL DEFAULT 1,
    is_dev BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (ingest_item_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_kb_chunks_document ON kb.chunks (document_id);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_tsv ON kb.chunks USING GIN (tsv);

CREATE OR REPLACE FUNCTION kb._notify_etl() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('kb_ingest', NEW.id::text);
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_kb_entries_notify ON kb.entries;
CREATE TRIGGER trg_kb_entries_notify
AFTER INSERT OR UPDATE OF content, meta
ON kb.entries FOR EACH ROW EXECUTE FUNCTION kb._notify_etl();

CREATE OR REPLACE FUNCTION kb._touch_entries_updated_at() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_kb_entries_touch ON kb.entries;
CREATE TRIGGER trg_kb_entries_touch
BEFORE UPDATE ON kb.entries FOR EACH ROW EXECUTE FUNCTION kb._touch_entries_updated_at();

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
    embedding VECTOR(1024),
    score_meta JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    version INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (chunk_id, space_id)
);

ALTER TABLE kb.chunk_embeddings
    ALTER COLUMN embedding TYPE VECTOR(1024)
    USING embedding::VECTOR(1024);

CREATE TABLE IF NOT EXISTS kb.document_embeddings (
    document_id UUID REFERENCES kb.documents(id) ON DELETE CASCADE,
    space_id INTEGER REFERENCES kb.embedding_spaces(id) ON DELETE CASCADE,
    embedding VECTOR(1024),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    version INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (document_id, space_id)
);

ALTER TABLE kb.document_embeddings
    ALTER COLUMN embedding TYPE VECTOR(1024)
    USING embedding::VECTOR(1024);

CREATE TABLE IF NOT EXISTS kb.dev_documents_meta (
    document_id UUID PRIMARY KEY REFERENCES kb.documents(id) ON DELETE CASCADE,
    source_uri TEXT,
    file_name TEXT,
    ingest_item_id UUID REFERENCES kb.ingest_items(id) ON DELETE SET NULL,
    domain TEXT,
    domain_confidence NUMERIC,
    classifier_source TEXT,
    extra JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE kb.dev_documents_meta
    ADD COLUMN IF NOT EXISTS domain TEXT,
    ADD COLUMN IF NOT EXISTS domain_confidence NUMERIC,
    ADD COLUMN IF NOT EXISTS classifier_source TEXT;

CREATE TABLE IF NOT EXISTS kb.ingest_flow_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ingest_item_id UUID REFERENCES kb.ingest_items(id) ON DELETE SET NULL,
    flow_name TEXT NOT NULL,
    prefect_run_id TEXT,
    parameters JSONB DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'running',
    error TEXT,
    result JSONB,
    is_dev BOOLEAN NOT NULL DEFAULT FALSE,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE kb.ingest_flow_runs
    ADD COLUMN IF NOT EXISTS prefect_run_id TEXT,
    ADD COLUMN IF NOT EXISTS is_dev BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_kb_ingest_flow_runs_ingest_item
    ON kb.ingest_flow_runs (ingest_item_id);
CREATE INDEX IF NOT EXISTS idx_kb_ingest_flow_runs_flow_name
    ON kb.ingest_flow_runs (flow_name);
CREATE INDEX IF NOT EXISTS idx_kb_ingest_flow_runs_status
    ON kb.ingest_flow_runs (status);

CREATE OR REPLACE FUNCTION kb.search_entries(
    query_text TEXT,
    query_vec VECTOR(1024),
    k INT,
    w_text REAL DEFAULT 0.6,
    w_vec REAL DEFAULT 0.4
) RETURNS TABLE(
    id UUID,
    session_id UUID,
    ingest_item_id UUID,
    source TEXT,
    uri TEXT,
    title TEXT,
    snippet TEXT,
    text_score REAL,
    vec_score REAL,
    score REAL
) LANGUAGE plpgsql STABLE AS $$
DECLARE
    trimmed_query TEXT := NULLIF(btrim(query_text), '');
    target_k INT := COALESCE(k, 50);
BEGIN
    IF trimmed_query IS NULL THEN
        RETURN QUERY
        WITH base AS (
            SELECT
                e.id,
                e.session_id,
                e.ingest_item_id,
                e.source,
                e.uri,
                e.title,
                substring(e.content FOR 400) AS snippet,
                0::REAL AS text_score,
                CASE WHEN query_vec IS NULL OR e.embedding IS NULL
                     THEN 0::REAL
                     ELSE (1 - (e.embedding <=> query_vec))::REAL END AS vec_score
            FROM kb.entries AS e
        )
        SELECT
            base.id,
            base.session_id,
            base.ingest_item_id,
            base.source,
            base.uri,
            base.title,
            base.snippet,
            base.text_score,
            base.vec_score,
            (w_text * base.text_score + w_vec * base.vec_score) AS score
        FROM base
        ORDER BY score DESC
        LIMIT target_k;
    ELSE
        RETURN QUERY
        WITH base AS (
            SELECT
                e.id,
                e.session_id,
                e.ingest_item_id,
                e.source,
                e.uri,
                e.title,
                substring(e.content FOR 400) AS snippet,
                paradedb.score(e.id)::REAL AS text_score,
                CASE WHEN query_vec IS NULL OR e.embedding IS NULL
                     THEN 0::REAL
                     ELSE (1 - (e.embedding <=> query_vec))::REAL END AS vec_score
            FROM kb.entries AS e
            WHERE e.id @@@ paradedb.boolean(
                should => ARRAY[
                    paradedb.match('title', trimmed_query),
                    paradedb.match('summary', trimmed_query),
                    paradedb.match('content', trimmed_query)
                ]
            )
        )
        SELECT
            base.id,
            base.session_id,
            base.ingest_item_id,
            base.source,
            base.uri,
            base.title,
            base.snippet,
            base.text_score,
            base.vec_score,
            (w_text * base.text_score + w_vec * base.vec_score) AS score
        FROM base
        ORDER BY score DESC
        LIMIT target_k;
    END IF;
END $$;

ALTER TABLE kb.documents
    ADD COLUMN IF NOT EXISTS source_uri TEXT,
    ADD COLUMN IF NOT EXISTS file_name TEXT,
    ADD COLUMN IF NOT EXISTS summary TEXT,
    ADD COLUMN IF NOT EXISTS content_digest TEXT,
    ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

ALTER TABLE kb.chunks
    ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE kb.chunk_embeddings
    ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE kb.entries
    ADD COLUMN IF NOT EXISTS is_document BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS is_note BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS needs_metadata BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS is_chat BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS kb.entry_metadata (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entry_id UUID NOT NULL REFERENCES kb.entries(id) ON DELETE CASCADE,
    meta_type TEXT NOT NULL,
    data JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (entry_id, meta_type)
);

CREATE INDEX IF NOT EXISTS idx_kb_entry_metadata_entry ON kb.entry_metadata (entry_id);
CREATE INDEX IF NOT EXISTS idx_kb_entry_metadata_type ON kb.entry_metadata (meta_type);

ALTER TABLE kb.document_embeddings
    ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE kb.entries
    ADD COLUMN IF NOT EXISTS document_id UUID REFERENCES kb.documents(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS file_name TEXT;

CREATE TABLE IF NOT EXISTS task.projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_key TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    priority TEXT,
    document_id UUID REFERENCES kb.documents(id) ON DELETE SET NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    tags TEXT[] DEFAULT ARRAY[]::TEXT[],
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_task_projects_status
    ON task.projects (status);
CREATE INDEX IF NOT EXISTS idx_task_projects_priority
    ON task.projects (priority);

CREATE TABLE IF NOT EXISTS task.tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_key TEXT UNIQUE NOT NULL,
    project_id UUID REFERENCES task.projects(id) ON DELETE SET NULL,
    document_id UUID REFERENCES kb.documents(id) ON DELETE SET NULL,
    ingest_item_id UUID REFERENCES kb.ingest_items(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    task_type TEXT,
    priority TEXT,
    owner TEXT,
    notes TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    tags TEXT[] DEFAULT ARRAY[]::TEXT[],
    due_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    is_blocked BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_tasks_project
    ON task.tasks (project_id);
CREATE INDEX IF NOT EXISTS idx_task_tasks_status
    ON task.tasks (status);
CREATE INDEX IF NOT EXISTS idx_task_tasks_priority
    ON task.tasks (priority);
CREATE INDEX IF NOT EXISTS idx_task_tasks_owner
    ON task.tasks (owner);

CREATE TABLE IF NOT EXISTS task.dag_edges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID REFERENCES task.projects(id) ON DELETE CASCADE,
    from_task_id UUID NOT NULL REFERENCES task.tasks(id) ON DELETE CASCADE,
    to_task_id UUID NOT NULL REFERENCES task.tasks(id) ON DELETE CASCADE,
    edge_type TEXT NOT NULL DEFAULT 'depends_on',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, from_task_id, to_task_id, edge_type)
);

CREATE INDEX IF NOT EXISTS idx_task_dag_edges_project
    ON task.dag_edges (project_id);
CREATE INDEX IF NOT EXISTS idx_task_dag_edges_from_to
    ON task.dag_edges (from_task_id, to_task_id);

CREATE TABLE IF NOT EXISTS task.activity_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID REFERENCES task.tasks(id) ON DELETE CASCADE,
    project_id UUID REFERENCES task.projects(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    actor TEXT,
    source_flow TEXT,
    prefect_run_id TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_activity_log_task
    ON task.activity_log (task_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_activity_log_project
    ON task.activity_log (project_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS task.graph_sync_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID REFERENCES task.projects(id) ON DELETE SET NULL,
    project_key TEXT NOT NULL,
    deployment_name TEXT NOT NULL,
    flow_run_id UUID,
    status TEXT NOT NULL,
    message TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_graph_sync_runs_project
    ON task.graph_sync_runs (project_key, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_task_graph_sync_runs_flow
    ON task.graph_sync_runs (flow_run_id);

CREATE SCHEMA IF NOT EXISTS cfg;

CREATE TABLE IF NOT EXISTS cfg.providers (
    id SERIAL PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    display_name TEXT,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS cfg.models (
    id SERIAL PRIMARY KEY,
    provider_id INTEGER NOT NULL REFERENCES cfg.providers(id) ON DELETE CASCADE,
    alias TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    endpoint TEXT,
    purpose TEXT NOT NULL DEFAULT 'chat',
    dims INTEGER,
    default_params JSONB NOT NULL DEFAULT '{}'::jsonb,
    pricing JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS cfg.tools (
    id SERIAL PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL,
    manifest_or_ref TEXT,
    default_params JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS cfg.agents (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    type TEXT NOT NULL,
    model_alias TEXT,
    system_prompt TEXT,
    params JSONB NOT NULL DEFAULT '{}'::jsonb,
    tools_profile TEXT,
    db_scope JSONB NOT NULL DEFAULT '[]'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS cfg.agent_tools (
    agent_id INTEGER NOT NULL REFERENCES cfg.agents(id) ON DELETE CASCADE,
    tool_id INTEGER NOT NULL REFERENCES cfg.tools(id) ON DELETE CASCADE,
    overrides JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (agent_id, tool_id)
);

CREATE TABLE IF NOT EXISTS cfg.active (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS cfg.flags (
    name TEXT PRIMARY KEY,
    description TEXT,
    default_value BOOLEAN NOT NULL DEFAULT FALSE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO cfg.providers (slug, display_name, notes)
VALUES
    ('litellm', 'LiteLLM Router', 'LiteLLM proxy for chat and routing'),
    ('tei', 'Text Embeddings Inference', 'Local TEI embedding backends')
ON CONFLICT (slug) DO UPDATE
SET
    display_name = EXCLUDED.display_name,
    notes = EXCLUDED.notes,
    updated_at = NOW();

INSERT INTO cfg.models (
    provider_id,
    alias,
    name,
    endpoint,
    purpose,
    dims,
    default_params,
    enabled
)
SELECT provider_id, alias, name, endpoint, purpose, dims, params, enabled
FROM (
    VALUES
        ((SELECT id FROM cfg.providers WHERE slug = 'litellm'), 'planner', 'planner', '${LITELLM_BASE_URL}', 'chat', NULL, '{"temperature": 0.0}'::jsonb, TRUE),
        ((SELECT id FROM cfg.providers WHERE slug = 'litellm'), 'responder', 'responder', '${LITELLM_BASE_URL}', 'chat', NULL, '{"temperature": 0.2}'::jsonb, TRUE),
        ((SELECT id FROM cfg.providers WHERE slug = 'litellm'), 'cheap-worker', 'cheap-worker', '${LITELLM_BASE_URL}', 'chat', NULL, '{"temperature": 0.2}'::jsonb, TRUE),
        ((SELECT id FROM cfg.providers WHERE slug = 'tei'), 'emb-general', 'emb-general', '${TEI_GTE_LARGE_URL}', 'embedding', 1024, '{}'::jsonb, TRUE),
        ((SELECT id FROM cfg.providers WHERE slug = 'tei'), 'emb-legal', 'emb-legal', '${TEI_LEGAL_BERT_URL}', 'embedding', 1024, '{}'::jsonb, TRUE),
        ((SELECT id FROM cfg.providers WHERE slug = 'tei'), 'emb-code', 'emb-code', '${TEI_GTE_LARGE_URL}', 'embedding', 1024, '{}'::jsonb, TRUE),
        ((SELECT id FROM cfg.providers WHERE slug = 'litellm'), 'emo-twitter', 'twitter-roberta-base-emotion', '${LITELLM_BASE_URL}', 'classification', NULL, '{"temperature": 0.0}'::jsonb, TRUE),
        ((SELECT id FROM cfg.providers WHERE slug = 'litellm'), 'sent-twitter', 'twitter-roberta-base-sentiment-latest', '${LITELLM_BASE_URL}', 'classification', NULL, '{"temperature": 0.0}'::jsonb, TRUE)
) AS payload(provider_id, alias, name, endpoint, purpose, dims, params, enabled)
ON CONFLICT (alias) DO UPDATE
SET
    provider_id = EXCLUDED.provider_id,
    name = EXCLUDED.name,
    endpoint = EXCLUDED.endpoint,
    purpose = EXCLUDED.purpose,
    dims = EXCLUDED.dims,
    default_params = EXCLUDED.default_params,
    enabled = EXCLUDED.enabled,
    updated_at = NOW();

INSERT INTO cfg.tools (slug, kind, manifest_or_ref, default_params, enabled, notes)
VALUES
    ('db_search', 'native', 'builtin:db_search', '{}'::jsonb, TRUE, 'Hybrid ParadeDB search'),
    ('web_search', 'native', 'builtin:web_search', '{}'::jsonb, TRUE, 'SearxNG toolbox web search'),
    ('graph_search', 'mcp', 'mcp:graph', '{}'::jsonb, TRUE, 'Graph exploration via MCP'),
    ('neo4j_cypher', 'mcp', 'mcp:neo4j_cypher', '{}'::jsonb, TRUE, 'Neo4j Cypher MCP client'),
    ('neo4j_memory', 'mcp', 'mcp:neo4j_memory', '{}'::jsonb, TRUE, 'Neo4j memory MCP client'),
    ('neo4j_modeling', 'mcp', 'mcp:neo4j_modeling', '{}'::jsonb, TRUE, 'Neo4j modeling MCP client'),
    ('legal_search', 'mcp', 'mcp:legal', '{}'::jsonb, TRUE, 'Legal knowledge MCP search'),
    ('agent-sequentialthinking', 'mcp', 'mcp:sequentialthinking', '{}'::jsonb, TRUE, 'Sequential thinking MCP agent'),
    ('search-toolbox', 'http', '${SEARCH_TOOLBOX_BASE_URL}', '{}'::jsonb, TRUE, 'Custom SearxNG toolbox endpoint'),
    ('docling', 'http', '${DOCLING_BASE_URL}', '{}'::jsonb, TRUE, 'Docling document converter')
ON CONFLICT (slug) DO UPDATE
SET
    kind = EXCLUDED.kind,
    manifest_or_ref = EXCLUDED.manifest_or_ref,
    default_params = EXCLUDED.default_params,
    enabled = EXCLUDED.enabled,
    notes = EXCLUDED.notes,
    updated_at = NOW();

INSERT INTO cfg.active (key, value)
VALUES
    ('active_planner_model', 'planner'),
    ('active_responder_model', 'responder'),
    ('active_worker_model', 'cheap-worker'),
    ('active_emb_general', 'emb-general'),
    ('active_emb_legal', 'emb-legal'),
    ('active_emb_code', 'emb-code')
ON CONFLICT (key) DO UPDATE
SET
    value = EXCLUDED.value,
    updated_at = NOW();

INSERT INTO cfg.agents (
    name,
    type,
    model_alias,
    system_prompt,
    params,
    db_scope,
    enabled,
    notes
) VALUES
    (
        'planner',
        'planner',
        'planner',
        NULL,
        '{"model": "planner", "include_thoughts": false, "generation": {"response_mime_type": "text/plain", "max_output_tokens": 512, "thinking_mode": "dynamic", "thinking_budget_tokens": 2048}}'::jsonb,
        '["kb.entries","agent.search_sandbox"]'::jsonb,
        TRUE,
        'Primary planning agent'
    ),
    (
        'responder',
        'base',
        'responder',
        NULL,
        '{"model": "responder", "include_thoughts": false, "generation": {"response_mime_type": "text/plain", "max_output_tokens": 1024, "thinking_mode": "disabled", "thinking_budget_tokens": 512}}'::jsonb,
        '[]'::jsonb,
        TRUE,
        'Response synthesis agent'
    ),
    (
        'audit',
        'base',
        'responder',
        NULL,
        '{"model": "responder", "include_thoughts": false, "generation": {"response_mime_type": "text/plain", "max_output_tokens": 768, "thinking_mode": "disabled", "thinking_budget_tokens": 512}}'::jsonb,
        '[]'::jsonb,
        TRUE,
        'Audit agent leveraging responder model'
    ),
    (
        'cheap-worker',
        'custom',
        'cheap-worker',
        NULL,
        '{"model": "cheap-worker", "include_thoughts": false, "generation": {"response_mime_type": "text/plain", "max_output_tokens": 512, "thinking_mode": "disabled", "thinking_budget_tokens": 512}}'::jsonb,
        '[]'::jsonb,
        TRUE,
        'Low-cost worker agent'
    ),
    (
        'default-chat',
        'base',
        'responder',
        NULL,
        '{"model": "responder", "include_thoughts": false, "generation": {"response_mime_type": "text/plain", "max_output_tokens": 1024, "thinking_mode": "disabled", "thinking_budget_tokens": 512}}'::jsonb,
        '[]'::jsonb,
        TRUE,
        'Default chat session agent'
    )
ON CONFLICT (name) DO UPDATE
SET
    type = EXCLUDED.type,
    model_alias = EXCLUDED.model_alias,
    params = EXCLUDED.params,
    db_scope = EXCLUDED.db_scope,
    enabled = EXCLUDED.enabled,
    notes = EXCLUDED.notes,
    updated_at = NOW();

INSERT INTO cfg.agent_tools (agent_id, tool_id, overrides)
SELECT agents.id, tools.id, overrides
FROM (
    VALUES
        ('planner', 'db_search', '{}'::jsonb),
        ('planner', 'web_search', '{}'::jsonb),
        ('planner', 'graph_search', '{}'::jsonb),
        ('planner', 'neo4j_cypher', '{}'::jsonb),
        ('planner', 'neo4j_memory', '{}'::jsonb),
        ('planner', 'neo4j_modeling', '{}'::jsonb),
        ('planner', 'legal_search', '{}'::jsonb),
        ('planner', 'agent-sequentialthinking', '{}'::jsonb)
) AS mapping(agent_name, tool_slug, overrides)
JOIN cfg.agents AS agents ON agents.name = mapping.agent_name
JOIN cfg.tools AS tools ON tools.slug = mapping.tool_slug
ON CONFLICT (agent_id, tool_id) DO UPDATE
SET overrides = EXCLUDED.overrides;

CREATE SCHEMA IF NOT EXISTS agent;

CREATE TABLE IF NOT EXISTS agent.sessions (
    id UUID PRIMARY KEY,
    agent_id INTEGER REFERENCES cfg.agents(id) ON DELETE SET NULL,
    meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

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
    session_id UUID REFERENCES agent.sessions(id) ON DELETE SET NULL,
    raw JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_events_run ON agent.events (run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_events_session ON agent.events (session_id, created_at DESC);

CREATE TABLE IF NOT EXISTS agent.search_sandbox (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL,
    requirement_id TEXT,
    task_id TEXT,
    iteration INTEGER NOT NULL DEFAULT 0,
    rank INTEGER NOT NULL,
    source TEXT,
    title TEXT,
    url TEXT NOT NULL,
    snippet TEXT,
    raw_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    promoted BOOLEAN NOT NULL DEFAULT FALSE,
    promoted_at TIMESTAMPTZ,
    kb_document_id UUID,
    kb_chunk_ids UUID[],
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, iteration, url)
);

CREATE INDEX IF NOT EXISTS idx_search_sandbox_run_iteration
    ON agent.search_sandbox (run_id, iteration, rank);

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
    query_embedding VECTOR(1024),
    snippet_embedding VECTOR(1024),
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
