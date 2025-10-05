-- Extensions (names can vary by build; adjust to your ParadeDB image)
CREATE EXTENSION IF NOT EXISTS vector;
-- ParadeDB exposes BM25 indexes via pg_search (pg_bm25 binaries are not shipped)
CREATE EXTENSION IF NOT EXISTS pg_search;

-- Whole documents
CREATE TABLE IF NOT EXISTS docs (
  id           UUID PRIMARY KEY,
  slug         TEXT NOT NULL,
  version      INT  NOT NULL DEFAULT 1,
  source_file  TEXT,
  kind         TEXT,
  content_md   TEXT,
  token_count  INT,
  emb_general  VECTOR(1024),
  emb_legal    VECTOR(1024),
  created_at   TIMESTAMP DEFAULT now(),
  UNIQUE (slug, version)
);

-- Chunks
CREATE TABLE IF NOT EXISTS chunks (
  id           UUID PRIMARY KEY,
  doc_id       UUID REFERENCES docs(id) ON DELETE CASCADE,
  doc_version  INT  NOT NULL DEFAULT 1,
  version      INT  NOT NULL DEFAULT 1,
  section      TEXT,
  idx          INT,
  text         TEXT,
  page_from    INT,
  page_to      INT,
  token_count  INT,
  overlap_used INT,
  emb_general  VECTOR(1024),
  emb_legal    VECTOR(1024),
  created_at   TIMESTAMP DEFAULT now()
);

-- NER / tags per doc
CREATE TABLE IF NOT EXISTS doc_tags (
  doc_id      UUID REFERENCES docs(id) ON DELETE CASCADE,
  tag_key     TEXT,
  tag_value   TEXT
);

-- NER / tags per chunk
CREATE TABLE IF NOT EXISTS chunk_tags (
  chunk_id    UUID REFERENCES chunks(id) ON DELETE CASCADE,
  tag_key     TEXT,
  tag_value   TEXT
);

CREATE INDEX IF NOT EXISTS chunks_emb_legal_ix   ON chunks USING hnsw (emb_legal vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_emb_general_ix ON chunks USING hnsw (emb_general vector_cosine_ops);

-- Web sources (scraped HTML -> Markdown)
CREATE TABLE IF NOT EXISTS web (
  id           UUID PRIMARY KEY,
  url          TEXT NOT NULL,
  slug         TEXT NOT NULL,
  version      INT  NOT NULL DEFAULT 1,
  title        TEXT,
  content_md   TEXT,
  token_count  INT,
  emb_general  VECTOR(1024),
  emb_legal    VECTOR(1024),
  retrieved_at TIMESTAMP DEFAULT now(),
  UNIQUE (slug, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS web_url_version_idx ON web (url, version);

CREATE TABLE IF NOT EXISTS web_chunks (
  id           UUID PRIMARY KEY,
  web_id       UUID REFERENCES web(id) ON DELETE CASCADE,
  web_version  INT  NOT NULL DEFAULT 1,
  version      INT  NOT NULL DEFAULT 1,
  section      TEXT,
  idx          INT,
  text         TEXT,
  token_count  INT,
  overlap_used INT,
  emb_general  VECTOR(1024),
  emb_legal    VECTOR(1024),
  created_at   TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS web_chunks_emb_legal_ix   ON web_chunks USING hnsw (emb_legal vector_cosine_ops);
CREATE INDEX IF NOT EXISTS web_chunks_emb_general_ix ON web_chunks USING hnsw (emb_general vector_cosine_ops);

CREATE TABLE IF NOT EXISTS web_tags (
  web_id    UUID REFERENCES web(id) ON DELETE CASCADE,
  tag_key   TEXT,
  tag_value TEXT
);

CREATE TABLE IF NOT EXISTS web_chunk_tags (
  chunk_id  UUID REFERENCES web_chunks(id) ON DELETE CASCADE,
  tag_key   TEXT,
  tag_value TEXT
);
