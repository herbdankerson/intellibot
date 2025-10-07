import json
import sys
import types
from typing import List
from uuid import uuid4

import pytest

prefect_stub = types.SimpleNamespace(task=lambda fn: fn)
sys.modules.setdefault("prefect", prefect_stub)
litellm_stub = types.SimpleNamespace(completion=lambda **_: None, embedding=lambda **_: None)
sys.modules.setdefault("litellm", litellm_stub)

from etl.tasks import intake_models
from etl.tasks import intake_tasks
from etl.tasks.model_clients import (
    CODE_EMBED_MODEL,
    GENERAL_EMBED_MODEL,
    SUMMARIZER_MODEL,
    embed_with_code,
    embed_with_general,
    summarize_chunks_with_gemini,
)


class DummyResult:
    def __init__(self, fetch=None, scalar=None):
        self._fetch = fetch
        self._scalar = scalar

    def fetchone(self):
        return self._fetch

    def scalar_one(self):
        if self._scalar is None:
            raise AssertionError("scalar_one called without value")
        return self._scalar


class DummyConnection:
    def __init__(self, engine):
        self._engine = engine

    def execute(self, statement, params=None):
        self._engine.statements.append((str(statement), params))
        sql = str(statement)
        if "SELECT id FROM kb.embedding_spaces" in sql:
            return DummyResult(fetch=None)
        if "RETURNING id" in sql:
            return DummyResult(scalar=1)
        return DummyResult()


class DummyConnectionContext:
    def __init__(self, engine):
        self._engine = engine

    def __enter__(self):
        return DummyConnection(self._engine)

    def __exit__(self, exc_type, exc, tb):
        return False


class DummyEngine:
    def __init__(self):
        self.statements: List[tuple[str, dict | None]] = []
        self.dialect = type("Dialect", (), {"name": "postgresql"})()

    def begin(self):
        return DummyConnectionContext(self)


def test_summarize_chunks_with_gemini_parses_batch(monkeypatch):
    records = []

    def fake_completion(**kwargs):
        records.append(kwargs)
        return {"choices": [{"message": {"content": json.dumps(["A", "B"])}}]}

    monkeypatch.setattr("etl.tasks.model_clients.completion", fake_completion)

    result = summarize_chunks_with_gemini(["chunk1", "chunk2"], max_length=128)

    assert result == ["A", "B"]
    assert len(records) == 1
    assert records[0]["model"] == SUMMARIZER_MODEL


def test_embed_with_code_uses_litellm_embedding(monkeypatch):
    calls = []

    def fake_embedding(**kwargs):
        calls.append(kwargs)
        return {"data": [{"embedding": [0.1, 0.2, 0.3]}]}

    monkeypatch.setattr("etl.tasks.model_clients.embedding", fake_embedding)

    vectors = embed_with_code(["hello"])

    assert vectors == [[0.1, 0.2, 0.3]]
    assert calls[0]["input"] == ["hello"]
    assert calls[0]["model"] == CODE_EMBED_MODEL


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


def test_persist_results_updates_ingest_item(monkeypatch):
    engine = DummyEngine()
    monkeypatch.setattr(intake_tasks, "get_engine", lambda *args, **kwargs: engine)

    ingest_item = intake_models.new_ingest_item(
        source_type="document",
        source_uri="upload.pdf",
        display_name="Upload",
        metadata={},
    )
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
        text="Body",
        token_count=10,
        overlap_tokens=0,
        ner_entities=[],
        summary="Chunk summary",
    )
    embedding = intake_models.ChunkEmbedding(
        chunk_id=chunk.id,
        space="emb-general",
        model=GENERAL_EMBED_MODEL,
        vector=[0.1, 0.2],
    )

    report = intake_tasks.persist_results(
        ingest_item,
        document,
        [chunk],
        [embedding],
        abstractions=["Body"],
    )

    assert report.chunk_count == 1
    assert report.embedding_spaces == ["emb-general"]
    assert report.job_id == str(ingest_item.job_id)
    assert any("UPDATE kb.ingest_items" in stmt for stmt, _ in engine.statements)
    assert "chunk_abstractions" in report.ingest_item.metadata
