import json
import os
import socket
import sys
import types
from pathlib import Path
from typing import List
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

prefect_stub = types.SimpleNamespace(task=lambda fn: fn)
sys.modules.setdefault("prefect", prefect_stub)
litellm_stub = types.SimpleNamespace(completion=lambda **_: None, embedding=lambda **_: None)
sys.modules.setdefault("litellm", litellm_stub)

from etl.tasks import intake_models
from etl.tasks import intake_tasks
from etl.tasks import model_clients
from etl.tasks.model_clients import (
    embed_with_code,
    embed_with_general,
    summarize_chunks_with_gemini,
)
from src.my_agentic_chatbot.runtime_config import (
    get_runtime_config,
    refresh_runtime_config,
)
from src.my_agentic_chatbot.storage.connection import get_engine, run_sql_file


PROVIDER_ROWS = (
    {"slug": "litellm", "notes": "LiteLLM router"},
    {"slug": "tei", "notes": "Text Embeddings Inference"},
)

MODEL_ROWS = (
    {
        "alias": "planner-gemini-flash",
        "name": "planner-gemini-flash",
        "endpoint": "${LITELLM_BASE_URL}",
        "dims": None,
        "purpose": "chat",
        "enabled": True,
        "notes": "Gemini 2.5 Flash planner routed through LiteLLM",
        "default_params": {"temperature": 0},
        "provider_slug": "litellm",
        "identifier": "planner",
        "uri_template": "${LITELLM_BASE_URL}",
        "version": "2.5",
        "config": {},
        "pricing": {},
    },
    {
        "alias": "responder-gemini-flash",
        "name": "responder-gemini-flash",
        "endpoint": "${LITELLM_BASE_URL}",
        "dims": None,
        "purpose": "chat",
        "enabled": True,
        "notes": "Gemini 2.5 Flash responder routed through LiteLLM",
        "default_params": {"temperature": 0.2},
        "provider_slug": "litellm",
        "identifier": "responder",
        "uri_template": "${LITELLM_BASE_URL}",
        "version": "2.5",
        "config": {},
        "pricing": {},
    },
    {
        "alias": "worker-gemini-flash",
        "name": "worker-gemini-flash",
        "endpoint": "${LITELLM_BASE_URL}",
        "dims": None,
        "purpose": "chat",
        "enabled": True,
        "notes": "Gemini 2.5 Flash worker for summaries and classification",
        "default_params": {"temperature": 0.2},
        "provider_slug": "litellm",
        "identifier": "cheap-worker",
        "uri_template": "${LITELLM_BASE_URL}",
        "version": "2.5",
        "config": {},
        "pricing": {},
    },
    {
        "alias": "emb-general",
        "name": "emb-general",
        "endpoint": "${TEI_GTE_LARGE_URL}",
        "dims": 1024,
        "purpose": "embedding",
        "enabled": True,
        "notes": "General embeddings served via TEI gte-large",
        "default_params": {},
        "provider_slug": "tei",
        "identifier": "thenlper/gte-large",
        "uri_template": "${TEI_GTE_LARGE_URL}",
        "version": "1",
        "config": {"embedding": True},
        "pricing": {},
    },
    {
        "alias": "emb-legal",
        "name": "emb-legal",
        "endpoint": "${TEI_LEGAL_BERT_URL}",
        "dims": 1024,
        "purpose": "embedding",
        "enabled": True,
        "notes": "Legal embeddings served via TEI legal-bert",
        "default_params": {},
        "provider_slug": "tei",
        "identifier": "nlpaueb/legal-bert-base-uncased",
        "uri_template": "${TEI_LEGAL_BERT_URL}",
        "version": "1",
        "config": {"embedding": True},
        "pricing": {},
    },
    {
        "alias": "emb-code",
        "name": "emb-code",
        "endpoint": "${TEI_GTE_LARGE_URL}",
        "dims": 1024,
        "purpose": "embedding",
        "enabled": True,
        "notes": "Code embeddings alias served via TEI gte-large",
        "default_params": {},
        "provider_slug": "tei",
        "identifier": "thenlper/gte-large",
        "uri_template": "${TEI_GTE_LARGE_URL}",
        "version": "1",
        "config": {"embedding": True},
        "pricing": {},
    },
    {
        "alias": "emo-twitter",
        "name": "twitter-roberta-base-emotion",
        "endpoint": "${LITELLM_BASE_URL}",
        "dims": None,
        "purpose": "classification",
        "enabled": True,
        "notes": "Twitter emotion classifier via LiteLLM",
        "default_params": {"temperature": 0.0},
        "provider_slug": "litellm",
        "identifier": "twitter-roberta-base-emotion",
        "uri_template": "${LITELLM_BASE_URL}",
        "version": "1",
        "config": {},
        "pricing": {},
    },
    {
        "alias": "sent-twitter",
        "name": "twitter-roberta-base-sentiment-latest",
        "endpoint": "${LITELLM_BASE_URL}",
        "dims": None,
        "purpose": "classification",
        "enabled": True,
        "notes": "Twitter sentiment classifier via LiteLLM",
        "default_params": {"temperature": 0.0},
        "provider_slug": "litellm",
        "identifier": "twitter-roberta-base-sentiment-latest",
        "uri_template": "${LITELLM_BASE_URL}",
        "version": "1",
        "config": {},
        "pricing": {},
    },
)

ACTIVE_ROWS = (
    {"key": "active_planner_model", "value": "planner-gemini-flash"},
    {"key": "active_responder_model", "value": "responder-gemini-flash"},
    {"key": "active_worker_model", "value": "worker-gemini-flash"},
    {"key": "active_emb_general", "value": "emb-general"},
    {"key": "active_emb_legal", "value": "emb-legal"},
    {"key": "active_emb_code", "value": "emb-code"},
)

TOOL_ROWS = (
    {
        "slug": "search-toolbox",
        "kind": "native",
        "manifest_or_ref": "${SEARCH_TOOLBOX_BASE_URL}",
        "default_params": {"method": "POST", "timeout": 15},
        "enabled": True,
        "notes": "Search toolbox HTTP endpoint",
    },
    {
        "slug": "docling",
        "kind": "native",
        "manifest_or_ref": "${DOCLING_BASE_URL}",
        "default_params": {"method": "POST", "timeout": 120},
        "enabled": True,
        "notes": "Docling normalization service",
    },
)


@pytest.fixture(scope="module", autouse=True)
def seed_runtime_config() -> None:
    try:
        run_sql_file(str(Path("src") / "my_agentic_chatbot" / "storage" / "models.sql"))
    except Exception:
        # The real database may be unavailable in unit test environments;
        # rely on per-test setup when migrations cannot run.
        pass
    os.environ.setdefault("TEI_GTE_LARGE_URL", "http://tei-gte-large:8080")
    os.environ.setdefault("TEI_LEGAL_BERT_URL", "http://tei-legal-bert:8080")
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        try:
            parsed = make_url(database_url)
            host = parsed.host
        except Exception:
            host = None
            parsed = None
        if host:
            try:
                socket.gethostbyname(host)
            except OSError:
                fallback = str(parsed.set(host="127.0.0.1")) if parsed else None
                if fallback:
                    os.environ["DATABASE_URL"] = fallback
    else:
        os.environ["DATABASE_URL"] = "postgresql+psycopg://agent:agentpass@127.0.0.1:5432/agentdb"
    engine = get_engine(os.environ["DATABASE_URL"])
    schema_bootstrap = (
        "CREATE SCHEMA IF NOT EXISTS cfg",
        """
        CREATE TABLE IF NOT EXISTS cfg.providers (
            id SERIAL PRIMARY KEY,
            slug TEXT UNIQUE NOT NULL,
            notes TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS cfg.models (
            id SERIAL PRIMARY KEY,
            alias TEXT UNIQUE,
            name TEXT UNIQUE NOT NULL,
            endpoint TEXT,
            dims INTEGER,
            purpose TEXT NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            notes TEXT,
            default_params JSONB NOT NULL DEFAULT '{}'::jsonb,
            provider_id INTEGER REFERENCES cfg.providers(id),
            pricing JSONB NOT NULL DEFAULT '{}'::jsonb,
            provider TEXT,
            identifier TEXT,
            uri_template TEXT,
            version TEXT,
            config JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS cfg.tools (
            id SERIAL PRIMARY KEY,
            slug TEXT UNIQUE NOT NULL,
            kind TEXT NOT NULL,
            manifest_or_ref TEXT NOT NULL,
            default_params JSONB NOT NULL DEFAULT '{}'::jsonb,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            notes TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS cfg.active (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
    )
    column_fixes = (
        "ALTER TABLE cfg.models ALTER COLUMN config SET DEFAULT '{}'::jsonb",
        "UPDATE cfg.models SET config = '{}'::jsonb WHERE config IS NULL",
        "ALTER TABLE cfg.models ALTER COLUMN pricing SET DEFAULT '{}'::jsonb",
        "UPDATE cfg.models SET pricing = '{}'::jsonb WHERE pricing IS NULL",
        "ALTER TABLE cfg.models ALTER COLUMN default_params SET DEFAULT '{}'::jsonb",
        "UPDATE cfg.models SET default_params = '{}'::jsonb WHERE default_params IS NULL",
        "ALTER TABLE cfg.models ALTER COLUMN enabled SET DEFAULT TRUE",
        "UPDATE cfg.models SET enabled = TRUE WHERE enabled IS NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_cfg_models_alias ON cfg.models (alias) WHERE alias IS NOT NULL",
    )
    kb_adjustments = (
        "ALTER TABLE IF EXISTS kb.chunk_embeddings ALTER COLUMN embedding TYPE VECTOR(1024) USING embedding::VECTOR(1024)",
        "ALTER TABLE IF EXISTS kb.document_embeddings ALTER COLUMN embedding TYPE VECTOR(1024) USING embedding::VECTOR(1024)",
    )
    with engine.begin() as conn:
        for statement in schema_bootstrap:
            conn.execute(text(statement))
        for statement in column_fixes:
            conn.execute(text(statement))
        for statement in kb_adjustments:
            conn.execute(text(statement))
        for provider in PROVIDER_ROWS:
            conn.execute(
                text(
                    """
                    INSERT INTO cfg.providers (slug, notes)
                    VALUES (:slug, :notes)
                    ON CONFLICT (slug) DO UPDATE
                    SET notes = EXCLUDED.notes,
                        updated_at = NOW()
                    """
                ),
                provider,
            )
        provider_map = {
            row["slug"]: row["id"]
            for row in conn.execute(
                text(
                    "SELECT id, slug FROM cfg.providers WHERE slug IN (:litellm, :tei)"
                ),
                {"litellm": "litellm", "tei": "tei"},
            ).mappings()
        }
        for row in MODEL_ROWS:
            provider_id = provider_map[row["provider_slug"]]
            params = {
                "alias": row["alias"],
                "name": row["name"],
                "endpoint": row["endpoint"],
                "dims": row["dims"],
                "purpose": row["purpose"],
                "enabled": row["enabled"],
                "notes": row["notes"],
                "default_params": json.dumps(row["default_params"]),
                "provider_id": provider_id,
                "pricing": json.dumps(row["pricing"]),
                "provider": row["provider_slug"],
                "identifier": row["identifier"],
                "uri_template": row["uri_template"],
                "version": row["version"],
                "config": json.dumps(row["config"]),
            }
            conn.execute(
                text(
                    """
                    INSERT INTO cfg.models (
                        alias, name, endpoint, dims, purpose, enabled, notes,
                        default_params, provider_id, pricing, provider,
                        identifier, uri_template, version, config
                    )
                    VALUES (
                        :alias, :name, :endpoint, :dims, :purpose, :enabled,
                        :notes, CAST(:default_params AS JSONB), :provider_id, CAST(:pricing AS JSONB),
                        :provider, :identifier, :uri_template, :version, CAST(:config AS JSONB)
                    )
                    ON CONFLICT (name) DO UPDATE
                    SET alias = EXCLUDED.alias,
                        endpoint = EXCLUDED.endpoint,
                        dims = EXCLUDED.dims,
                        purpose = EXCLUDED.purpose,
                        enabled = EXCLUDED.enabled,
                        notes = EXCLUDED.notes,
                        default_params = EXCLUDED.default_params,
                        provider_id = EXCLUDED.provider_id,
                        pricing = EXCLUDED.pricing,
                        provider = EXCLUDED.provider,
                        identifier = EXCLUDED.identifier,
                        uri_template = EXCLUDED.uri_template,
                        version = EXCLUDED.version,
                        config = EXCLUDED.config,
                        updated_at = NOW()
                    """
                ),
                params,
            )
        for row in ACTIVE_ROWS:
            conn.execute(
                text(
                    """
                    INSERT INTO cfg.active (key, value)
                    VALUES (:key, :value)
                    ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value,
                        updated_at = NOW()
                    """
                ),
                row,
            )
        for row in TOOL_ROWS:
            conn.execute(
                text(
                    """
                    INSERT INTO cfg.tools (slug, kind, manifest_or_ref, default_params, enabled, notes)
                    VALUES (:slug, :kind, :manifest_or_ref, CAST(:default_params AS JSONB), :enabled, :notes)
                    ON CONFLICT (slug) DO UPDATE
                    SET kind = EXCLUDED.kind,
                        manifest_or_ref = EXCLUDED.manifest_or_ref,
                        default_params = EXCLUDED.default_params,
                        enabled = EXCLUDED.enabled,
                        notes = EXCLUDED.notes,
                        updated_at = NOW()
                    """
                ),
                {
                    "slug": row["slug"],
                    "kind": row["kind"],
                    "manifest_or_ref": row["manifest_or_ref"],
                    "default_params": json.dumps(row["default_params"]),
                    "enabled": row["enabled"],
                    "notes": row["notes"],
                },
            )
    refresh_runtime_config()
    yield
    refresh_runtime_config()


@pytest.fixture
def ingest_item_record():
    ingest_item = intake_models.new_ingest_item(
        source_type="document",
        source_uri="upload.pdf",
        display_name="Upload",
        metadata={}
    )
    engine = get_engine()
    params = {
        "id": str(ingest_item.id),
        "job_id": str(ingest_item.job_id),
        "source_type": ingest_item.source_type,
        "source_uri": ingest_item.source_uri,
        "display_name": ingest_item.display_name,
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO kb.ingest_items (id, job_id, source_type, source_uri, display_name, status, metadata)
                VALUES (CAST(:id AS UUID), CAST(:job_id AS UUID), :source_type, :source_uri, :display_name, 'registered', '{}'::jsonb)
                """
            ),
            params,
        )
    try:
        yield ingest_item
    finally:
        cleanup_params = {"ingest_item_id": params["id"]}
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    DELETE FROM kb.chunk_embeddings
                    WHERE chunk_id IN (
                        SELECT id FROM kb.chunks WHERE ingest_item_id = CAST(:ingest_item_id AS UUID)
                    )
                    """
                ),
                cleanup_params,
            )
            conn.execute(
                text(
                    """
                    DELETE FROM kb.document_embeddings
                    WHERE document_id IN (
                        SELECT id FROM kb.documents WHERE ingest_item_id = CAST(:ingest_item_id AS UUID)
                    )
                    """
                ),
                cleanup_params,
            )
            conn.execute(
                text("DELETE FROM kb.entries WHERE ingest_item_id = CAST(:ingest_item_id AS UUID)"),
                cleanup_params,
            )
            conn.execute(
                text("DELETE FROM kb.chunks WHERE ingest_item_id = CAST(:ingest_item_id AS UUID)"),
                cleanup_params,
            )
            conn.execute(
                text("DELETE FROM kb.documents WHERE ingest_item_id = CAST(:ingest_item_id AS UUID)"),
                cleanup_params,
            )
            conn.execute(
                text("DELETE FROM kb.ingest_items WHERE id = CAST(:ingest_item_id AS UUID)"),
                cleanup_params,
            )


def test_summarize_chunks_with_gemini_parses_batch(monkeypatch):
    records = []

    def fake_summarize_chunks(texts, *, max_length, **kwargs):
        records.append({"texts": list(texts), "max_length": max_length, "kwargs": kwargs})
        return ["A", "B"]

    monkeypatch.setattr(
        "etl.tasks.model_clients.fresh_ingestion.summarize_chunks",
        fake_summarize_chunks,
    )

    result = summarize_chunks_with_gemini(["chunk1", "chunk2"], max_length=128)

    assert result == ["A", "B"]
    assert records == [
        {"texts": ["chunk1", "chunk2"], "max_length": 128, "kwargs": {}}
    ]


def test_embed_with_code_routes_through_provider(monkeypatch):
    runtime_config = get_runtime_config()
    calls: List[tuple] = []

    def fake_provider(*, model, inputs):
        calls.append((model, list(inputs)))
        return [[0.1, 0.2, 0.3]]

    monkeypatch.setattr(model_clients, "_call_embedding_provider", fake_provider)

    vectors = embed_with_code(["hello"])

    assert vectors == [[0.1, 0.2, 0.3]]
    assert len(calls) == 1
    assert calls[0][0] is runtime_config.active("active_emb_code")
    assert calls[0][1] == ["hello"]


def test_acquire_source_fetches_remote(monkeypatch):
    item = intake_models.new_ingest_item(
        source_type="website",
        source_uri="https://example.com/page",
        display_name="Example",
        metadata={},
    )

    class Response:
        status_code = 200
        content = b"hello world"

        def __init__(self):
            self.headers = {"content-type": "text/plain"}
            self.url = "https://example.com/page"

    monkeypatch.setattr(intake_tasks.httpx, "get", lambda *args, **kwargs: Response())

    acquired = intake_tasks.acquire_source(item, content=None)

    assert acquired.content == b"hello world"
    assert acquired.metadata["content_type"] == "text/plain"
    assert acquired.metadata["content_length"] == len(b"hello world")


def test_docling_normalize_uses_conversion(monkeypatch):
    source = intake_models.AcquiredSource(
        ingest_item_id=uuid4(),
        content=b"body",
        metadata={"filename": "doc.txt", "content_type": "text/plain"},
    )

    def fake_convert(*args, **kwargs):
        return ("# Title", "Plain text", {"language": "en", "title": "Doc"})

    monkeypatch.setattr(intake_tasks, "_docling_convert", fake_convert)

    normalized = intake_tasks.docling_normalize(source)

    assert normalized.markdown == "# Title"
    assert normalized.text == "Plain text"
    assert normalized.metadata["language"] == "en"


def test_persist_results_updates_ingest_item(ingest_item_record):
    runtime_config = get_runtime_config()

    ingest_item = ingest_item_record
    ingest_item.domain = "general"
    ingest_item.domain_confidence = 0.9
    ingest_item.document_summary = "Summary"

    document = intake_models.NormalizedDocument(
        ingest_item_id=ingest_item.id,
        markdown="# Heading",
        text="Heading\nBody",
        metadata={"language": "en", "source": {"content_type": "text/plain", "content_length": 12}},
    )

    chunk = intake_models.Chunk(
        id=uuid4(),
        ingest_item_id=ingest_item.id,
        document_id=None,
        chunk_index=0,
        heading_path=["Heading"],
        kind="paragraph",
        text=f"Body {ingest_item.id}",
        token_count=10,
        overlap_tokens=0,
        ner_entities=[],
        summary="Chunk summary",
    )

    pad_length = runtime_config.active("active_emb_general").require_dims()
    vector = [0.1, 0.2] + [0.0] * (pad_length - 2)
    embedding = intake_models.ChunkEmbedding(
        chunk_id=chunk.id,
        space=runtime_config.active("active_emb_general").name,
        model=runtime_config.active("active_emb_general").identifier,
        vector=vector,
    )

    chunk_emotions = {
        chunk.chunk_index: [
            {
                "type": "emotion",
                "label": "joy",
                "confidence": 0.9,
                "model": "twitter-roberta-base-emotion",
            }
        ]
    }

    report = intake_tasks.persist_results(
        ingest_item,
        document,
        [chunk],
        [embedding],
        chunk_emotions,
        abstractions=["Body"],
    )

    assert report.chunk_count == 1
    assert report.embedding_spaces == [runtime_config.active("active_emb_general").name]
    assert report.job_id == str(ingest_item.job_id)
    assert "chunk_abstractions" in report.ingest_item.metadata

    engine = get_engine()
    params = {"ingest_item_id": str(ingest_item.id)}
    with engine.begin() as conn:
        chunk_count = conn.execute(
            text("SELECT COUNT(*) FROM kb.chunks WHERE ingest_item_id = CAST(:ingest_item_id AS UUID)"),
            params,
        ).scalar_one()
        entry_count = conn.execute(
            text("SELECT COUNT(*) FROM kb.entries WHERE ingest_item_id = CAST(:ingest_item_id AS UUID)"),
            params,
        ).scalar_one()
        ingest_metadata = conn.execute(
            text("SELECT metadata FROM kb.ingest_items WHERE id = CAST(:ingest_item_id AS UUID)"),
            params,
        ).fetchone()

    assert chunk_count == 1
    assert entry_count >= 1
    assert ingest_metadata is not None
    metadata_payload = ingest_metadata[0]
    assert metadata_payload.get("chunk_abstractions")
