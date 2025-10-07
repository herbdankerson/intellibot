"""Regression tests that enforce the no-literals policy and runtime wiring."""

from __future__ import annotations

import types
from pathlib import Path
from typing import Iterable, List
from uuid import uuid4

from etl.tasks.intake_models import FlowReport


class _StubModelConfig:
    """Minimal stand-in for ``runtime_config.ModelConfig`` used in tests."""

    def __init__(self, name: str, identifier: str, dims: int) -> None:
        self.name = name
        self.identifier = identifier
        self.provider = "test"
        self.config: dict[str, object] = {}
        self._dims = dims
        self.purpose = "embedding"
        self.enabled = True

    def require_dims(self) -> int:
        return self._dims


class _StubRuntimeConfig:
    def __init__(self) -> None:
        self._models = {
            "general": _StubModelConfig("cfg-general", "general-model", 2),
            "legal": _StubModelConfig("cfg-legal", "legal-model", 3),
            "code": _StubModelConfig("cfg-code", "code-model", 4),
        }
        self.active_models = {
            "active_emb_general": self._models["general"],
            "active_emb_legal": self._models["legal"],
            "active_emb_code": self._models["code"],
        }

    def active(self, key: str) -> _StubModelConfig:
        return self.active_models[key]

    def model(self, name: str) -> _StubModelConfig:
        return self._models[name]


def test_ingest_web_capture_uses_active_spaces(monkeypatch):
    """Ensure web ingestion writes embeddings using the configured space names."""

    # --- runtime + dependency stubs -------------------------------------------------
    stub_runtime = _StubRuntimeConfig()
    monkeypatch.setattr(
        "src.my_agentic_chatbot.ingestion.web_ingest.get_runtime_config",
        lambda: stub_runtime,
    )

    call_log: list[list[str]] = []

    def fake_embed(texts: Iterable[str], *, model_key: str) -> List[List[float]]:
        cfg = stub_runtime.active(model_key)
        dims = cfg.require_dims()
        vector = [float(i + 1) for i in range(dims)]
        return [vector[:] for _ in texts]

    monkeypatch.setattr(
        "src.my_agentic_chatbot.ingestion.web_ingest.embed_with_general",
        lambda texts: fake_embed(texts, model_key="active_emb_general"),
    )
    monkeypatch.setattr(
        "src.my_agentic_chatbot.ingestion.web_ingest.embed_with_legal",
        lambda texts: fake_embed(texts, model_key="active_emb_legal"),
    )
    monkeypatch.setattr(
        "src.my_agentic_chatbot.ingestion.web_ingest.embed_with_code",
        lambda texts: fake_embed(texts, model_key="active_emb_code"),
    )

    # Avoid network/LLM calls during the test.
    monkeypatch.setattr(
        "src.my_agentic_chatbot.ingestion.web_ingest.summarize_with_gemini",
        lambda *_, **__: "summary",
    )
    monkeypatch.setattr(
        "src.my_agentic_chatbot.ingestion.web_ingest.summarize_chunks_with_gemini",
        lambda texts, *, max_length=256: ["chunk summary" for _ in texts],
    )
    monkeypatch.setattr(
        "src.my_agentic_chatbot.ingestion.web_ingest.chunker.build_chunks",
        lambda *_args, **_kwargs: [
            {"idx": 0, "title": "Doc", "text": "Sample", "token_count": 10, "overlap": 0}
        ],
    )

    class _DummyConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, *_args, **_kwargs):
            return types.SimpleNamespace(scalar_one=lambda: 1)

    class _DummyEngine:
        def begin(self):
            return _DummyConnection()

    monkeypatch.setattr(
        "src.my_agentic_chatbot.ingestion.web_ingest.get_engine",
        lambda: _DummyEngine(),
    )

    def fake_persist_results(item, document, chunks, embeddings, abstractions):
        spaces = sorted({embedding.space for embedding in embeddings})
        call_log.append(spaces)
        return FlowReport(
            ingest_item=item,
            chunk_count=len(chunks),
            embedding_spaces=spaces,
            job_id="job",
            metadata={"document_id": str(uuid4())},
        )

    monkeypatch.setattr(
        "src.my_agentic_chatbot.ingestion.web_ingest.persist_results",
        types.SimpleNamespace(fn=fake_persist_results),
    )

    from src.my_agentic_chatbot.ingestion.web_ingest import ingest_web_capture

    ingest_web_capture(
        run_id="run",
        requirement_id="req",
        url="https://example.com/legal",
        display_name="Legal",
        markdown="# Law",
        plain_text="Law text",
        curated_summary=None,
        metadata=None,
        domain="legal",
    )

    ingest_web_capture(
        run_id="run",
        requirement_id="req",
        url="https://example.com/code",
        display_name="Code",
        markdown="# Code",
        plain_text="Code text",
        curated_summary=None,
        metadata=None,
        domain="code",
    )

    assert call_log[0] == ["cfg-general", "cfg-legal"]
    assert call_log[1] == ["cfg-code", "cfg-general"]


def test_required_active_keys():
    from src.my_agentic_chatbot import runtime_config

    expected = {
        "active_planner_model",
        "active_responder_model",
        "active_worker_model",
        "active_emb_general",
        "active_emb_legal",
        "active_emb_code",
    }

    assert runtime_config._REQUIRED_ACTIVE_KEYS == expected


def test_no_embedding_literals_in_runtime_code():
    """Ensure runtime directories don't contain forbidden literal identifiers."""

    repo_root = Path(__file__).resolve().parents[1]
    search_roots = [repo_root / "src", repo_root / "etl", repo_root / "ops" / "scripts"]
    forbidden_tokens = ["emb-general", "emb-legal", "emb-code", "http://tei-"]
    offenders: list[str] = []

    for root in search_roots:
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".pyi", ".yaml", ".yml"}:
                continue
            relative = path.relative_to(repo_root)
            text = path.read_text(encoding="utf-8")
            for token in forbidden_tokens:
                if token in text:
                    offenders.append(f"{relative}: contains '{token}'")

    assert not offenders, "Forbidden literals detected:\n" + "\n".join(offenders)
