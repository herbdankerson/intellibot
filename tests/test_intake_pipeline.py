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
from etl.tasks import model_clients
from etl.tasks.model_clients import (
    embed_with_code,
    embed_with_general,
    summarize_chunks_with_gemini,
)
from src.my_agentic_chatbot.runtime_config import ToolConfig


class StubModelConfig:
    def __init__(
        self,
        name: str,
        identifier: str,
        *,
        dims: int | None,
        provider: str = "test",
        purpose: str = "embedding",
    ) -> None:
        self.name = name
        self.identifier = identifier
        self.uri_template = None
        self.resolved_uri = None
        self.dims = dims
        self.provider = provider
        self.purpose = purpose
        self.enabled = True
        self.version = "test"
        self.notes = None
        self.config: dict[str, object] = {}

    def require_dims(self) -> int:
        if self.dims is None:
            raise RuntimeError(f"Model {self.name} missing dims in test stub")
        return self.dims


class StubRuntimeConfig:
    def __init__(
        self,
        models: dict[str, StubModelConfig],
        active_models: dict[str, StubModelConfig],
        tools: dict[str, ToolConfig] | None = None,
    ):
        self.models = models
        self.active_models = active_models
        self.tools = tools or {}

    def model(self, name: str) -> StubModelConfig:
        return self.models[name]

    def active(self, key: str) -> StubModelConfig:
        return self.active_models[key]


@pytest.fixture(autouse=True)
def runtime_config_stub(monkeypatch):
    general = StubModelConfig("emb-general", "emb-general", dims=2)
    legal = StubModelConfig("emb-legal", "emb-legal", dims=2)
    code = StubModelConfig("emb-code", "emb-code", dims=3)
    worker = StubModelConfig("worker-local-smollm", "cheap-worker", dims=None, purpose="chat")
    planner = StubModelConfig("planner-local-smollm", "planner", dims=None, purpose="chat")
    responder = StubModelConfig("responder-local-smollm", "responder", dims=None, purpose="chat")

    models = {
        cfg.name: cfg
        for cfg in [general, legal, code, worker, planner, responder]
    }
    active_models = {
        "active_emb_general": general,
        "active_emb_legal": legal,
        "active_emb_code": code,
        "active_worker_model": worker,
        "active_planner_model": planner,
        "active_responder_model": responder,
    }
    toolbox = ToolConfig(
        name="search-toolbox",
        type="http",
        endpoint_template="http://toolbox",
        resolved_endpoint="http://toolbox",
        method="POST",
        auth_ref=None,
        timeout_s=10,
        config={},
        enabled=True,
    )
    docling_tool = ToolConfig(
        name="docling",
        type="http",
        endpoint_template="http://docling",
        resolved_endpoint="http://docling",
        method="POST",
        auth_ref=None,
        timeout_s=20,
        config={},
        enabled=True,
    )
    stub = StubRuntimeConfig(
        models=models,
        active_models=active_models,
        tools={toolbox.name: toolbox, docling_tool.name: docling_tool},
    )
    monkeypatch.setattr(intake_tasks, "get_runtime_config", lambda: stub)
    monkeypatch.setattr(model_clients, "get_runtime_config", lambda: stub)
    monkeypatch.setattr("src.my_agentic_chatbot.runtime_config.get_runtime_config", lambda: stub)
    return stub


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


def test_summarize_chunks_with_gemini_parses_batch(monkeypatch, runtime_config_stub):
    records = []

    def fake_completion(**kwargs):
        records.append(kwargs)
        return {"choices": [{"message": {"content": json.dumps(["A", "B"])}}]}

    monkeypatch.setattr("etl.tasks.model_clients.completion", fake_completion)

    result = summarize_chunks_with_gemini(["chunk1", "chunk2"], max_length=128)

    assert result == ["A", "B"]
    assert len(records) == 1
    assert (
        records[0]["model"]
        == runtime_config_stub.active("active_worker_model").identifier
    )


def test_embed_with_code_uses_litellm_embedding(monkeypatch, runtime_config_stub):
    calls = []

    def fake_embedding(**kwargs):
        calls.append(kwargs)
        return {"data": [{"embedding": [0.1, 0.2, 0.3]}]}

    monkeypatch.setattr("etl.tasks.model_clients.embedding", fake_embedding)

    vectors = embed_with_code(["hello"])

    assert vectors == [[0.1, 0.2, 0.3]]
    assert calls[0]["input"] == ["hello"]
    assert calls[0]["model"] == runtime_config_stub.active("active_emb_code").identifier


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


def test_persist_results_updates_ingest_item(monkeypatch, runtime_config_stub):
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
    general_model = runtime_config_stub.active("active_emb_general")
    embedding = intake_models.ChunkEmbedding(
        chunk_id=chunk.id,
        space=general_model.name,
        model=general_model.identifier,
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
    assert report.embedding_spaces == [general_model.name]
    assert report.job_id == str(ingest_item.job_id)
    assert any("UPDATE kb.ingest_items" in stmt for stmt, _ in engine.statements)
    assert "chunk_abstractions" in report.ingest_item.metadata
