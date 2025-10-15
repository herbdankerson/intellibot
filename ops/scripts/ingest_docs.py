"""Ingest local documents through the ETL pipeline.

This utility defaults to a stubbed mode so we can exercise the ingestion
pipeline without relying on the live Postgres instance or external model
services. Pass ``--use-live`` to hit the real database and runtime config.
"""

from __future__ import annotations

import argparse
import mimetypes
import os
from pathlib import Path
import sys
from typing import Any, Dict, Iterable, List, Sequence

from sqlalchemy import text

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.append(str(BASE_DIR))

def iter_files(paths: Sequence[Path]) -> Iterable[Path]:
    for path in paths:
        if path.is_dir():
            for candidate in sorted(path.rglob("*")):
                if candidate.is_file():
                    yield candidate
        elif path.is_file():
            yield path


def enable_stub_mode() -> None:
    """Replace DB and model calls with deterministic stubs."""

    from types import SimpleNamespace

    from etl.tasks import intake_tasks as intake_tasks_mod
    from etl.tasks import model_clients
    from src.my_agentic_chatbot.storage import connection as db_connection
    from src.my_agentic_chatbot.runtime_config import (
        ModelConfig,
        RuntimeConfig,
    )
    from src.my_agentic_chatbot import runtime_config as runtime_config_mod
    from src.my_agentic_chatbot.ingestion import web_ingest

    class StubResult:
        def __init__(self, rows: Iterable[Dict[str, Any]] | None = None, scalar: Any = 0) -> None:
            self._rows = list(rows or [])
            self._scalar = scalar

        def fetchall(self):
            return list(self._rows)

        def fetchone(self):
            return self._rows[0] if self._rows else None

        def scalar_one(self):
            return self._scalar

        def mappings(self):
            return self

        def __iter__(self):
            return iter(self._rows)

    class StubConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, *_args, **_kwargs):
            return StubResult()

        def commit(self):
            return None

        def rollback(self):
            return None

    class StubEngine:
        dialect = SimpleNamespace(name="stub")

        def begin(self):
            return StubConnection()

        def connect(self):
            return StubConnection()

    stub_engine = StubEngine()

    def stub_get_engine(*_args, **_kwargs):
        return stub_engine

    db_connection.get_engine = stub_get_engine  # type: ignore
    intake_tasks_mod.get_engine = stub_get_engine  # type: ignore
    web_ingest.get_engine = stub_get_engine  # type: ignore

    lite_base = os.getenv("LITELLM_BASE_URL", "http://localhost:4000")
    tei_general = os.getenv("TEI_GTE_LARGE_URL", "http://localhost:6060")
    tei_legal = os.getenv("TEI_LEGAL_BERT_URL", "http://localhost:6061")

    def build_model(
        name: str,
        *,
        provider: str,
        identifier: str,
        endpoint: str,
        purpose: str,
        dims: int | None = None,
    ) -> ModelConfig:
        return ModelConfig(
            name=name,
            provider=provider,
            identifier=identifier,
            uri_template=endpoint,
            resolved_uri=endpoint,
            dims=dims,
            purpose=purpose,
            enabled=True,
            version=None,
            notes=None,
            config={},
        )

    planner_model = build_model("planner", provider="litellm", identifier="planner", endpoint=lite_base, purpose="chat")
    responder_model = build_model("responder", provider="litellm", identifier="responder", endpoint=lite_base, purpose="chat")
    worker_model = build_model("cheap-worker", provider="litellm", identifier="cheap-worker", endpoint=lite_base, purpose="chat")
    general_alias = "emb-" + "general"
    legal_alias = "emb-" + "legal"
    code_alias = "emb-" + "code"
    general_model = build_model(general_alias, provider="tei", identifier="thenlper/gte-large", endpoint=tei_general, purpose="embedding", dims=1024)
    legal_model = build_model(legal_alias, provider="tei", identifier="nlpaueb/legal-bert-base-uncased", endpoint=tei_legal, purpose="embedding", dims=1024)
    code_model = build_model(code_alias, provider="tei", identifier="thenlper/gte-large", endpoint=tei_general, purpose="embedding", dims=1024)
    emotion_model = build_model("emo-twitter", provider="litellm", identifier="twitter-roberta-base-emotion", endpoint=lite_base, purpose="classification")
    sentiment_model = build_model("sent-twitter", provider="litellm", identifier="twitter-roberta-base-sentiment-latest", endpoint=lite_base, purpose="classification")

    models = {
        model.name: model
        for model in (
            planner_model,
            responder_model,
            worker_model,
            general_model,
            legal_model,
            code_model,
            emotion_model,
            sentiment_model,
        )
    }

    active_models = {
        "active_planner_model": planner_model,
        "active_responder_model": responder_model,
        "active_worker_model": worker_model,
        "active_emb_general": general_model,
        "active_emb_legal": legal_model,
        "active_emb_code": code_model,
    }

    stub_runtime = RuntimeConfig(models=models, tools={}, active_models=active_models)

    def stub_get_runtime_config():
        return stub_runtime

    runtime_config_mod.get_runtime_config = stub_get_runtime_config  # type: ignore
    runtime_config_mod.refresh_runtime_config = lambda: stub_runtime  # type: ignore
    model_clients.get_runtime_config = stub_get_runtime_config  # type: ignore

    def fake_embed(texts: Sequence[str], dims: int = 1024) -> List[List[float]]:
        base = [0.01 * (index + 1) for index in range(8)]
        return [list((base * ((dims // len(base)) + 1))[:dims]) for _ in texts]

    model_clients.embed_with_general = lambda texts: fake_embed(texts, 1024)  # type: ignore
    model_clients.embed_with_legal = lambda texts: fake_embed(texts, 1024)  # type: ignore
    model_clients.embed_with_code = lambda texts: fake_embed(texts, 1024)  # type: ignore
    model_clients.summarize_with_gemini = lambda text, max_length=600: (text or "")[:max_length]  # type: ignore
    model_clients.summarize_chunks_with_gemini = (  # type: ignore
        lambda texts, max_length=256: [
            (text or "")[:max_length]
            for text in texts
        ]
    )
    model_clients.classify_domain = lambda _text: model_clients.ClassificationResult(domain="general", confidence=0.5, source_labels=["document"])  # type: ignore
    model_clients.analyze_emotions = lambda _text: [  # type: ignore
        {"type": "emotion", "label": "neutral", "confidence": 0.5, "model": "stub"}
    ]

    intake_tasks_mod.docling_normalize = lambda source: intake_tasks_mod.NormalizedDocument(  # type: ignore
        ingest_item_id=source.ingest_item_id,
        markdown=(source.content.decode("utf-8", errors="ignore") if isinstance(source.content, bytes) else str(source.content)),
        text=(source.content.decode("utf-8", errors="ignore") if isinstance(source.content, bytes) else str(source.content)),
        metadata={"language": "en", "source": source.metadata},
    )


def ingest_file(path: Path, *, use_live: bool) -> None:
    if not use_live:
        enable_stub_mode()
        print("[stub] Database and model calls replaced with deterministic stubs")

    from etl.flows.flow_document_intake import document_intake_flow
    from src.my_agentic_chatbot.storage.connection import get_engine

    payload = path.read_bytes()
    size_bytes = len(payload)
    mime_type, _ = mimetypes.guess_type(path.name)
    extra_metadata = {
        "source": {
            "filename": path.name,
            "path": str(path.resolve()),
            "content_type": mime_type or "application/octet-stream",
            "content_length": size_bytes,
        }
    }

    report = document_intake_flow(
        source_type="document",
        source_uri=str(path.resolve()),
        display_name=path.name,
        content=payload,
        extra_metadata=extra_metadata,
    )

    ingest_item_id = str(report.ingest_item.id)
    chunk_spaces = ", ".join(report.embedding_spaces) or "<none>"
    print(f"Ingested {path.name} → ingest_item={ingest_item_id}")
    print(
        f"  chunks={report.chunk_count} spaces=[{chunk_spaces}] domain={report.ingest_item.domain or 'unknown'}"
    )
    print(f"  metadata keys={sorted(report.metadata.keys())}")

    if use_live:
        engine = get_engine()
        with engine.connect() as conn:
            rows = list(
                conn.execute(
                    text(
                        """
                        SELECT
                            id::text AS id,
                            title,
                            embedding IS NOT NULL AS has_embedding,
                            COALESCE(jsonb_array_length(ner), 0) AS ner_count,
                            COALESCE(jsonb_array_length(emotions), 0) AS emotion_count
                        FROM kb.entries
                        WHERE ingest_item_id = CAST(:ingest_item_id AS UUID)
                        ORDER BY created_at
                        """
                    ),
                    {"ingest_item_id": ingest_item_id},
                ).mappings()
            )

        for index, row in enumerate(rows, start=1):
            status_bits: List[str] = []
            status_bits.append("vec" if row["has_embedding"] else "no-vec")
            status_bits.append("ner" if row["ner_count"] else "no-ner")
            status_bits.append("emo" if row["emotion_count"] else "no-emo")
            print(
                f"    entry[{index:02d}] id={row['id']} status={'/'.join(status_bits)} title={row.get('title') or '<untitled>'}"
            )

        if not rows:
            print("    WARNING: no kb.entries rows were generated for this ingest item.")
    else:
        print("  (stub mode) results stored in-memory only; no database rows were created")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest files using the ETL pipeline")
    parser.add_argument("paths", nargs="+", type=Path, help="Files or directories to ingest")
    parser.add_argument(
        "--use-live",
        action="store_true",
        help="Use the live database/runtime instead of the built-in stubs",
    )
    args = parser.parse_args()

    files = list(iter_files(args.paths))
    if not files:
        print("No documents found to ingest.")
        return

    for path in files:
        try:
            ingest_file(path, use_live=args.use_live)
        except Exception as exc:  # pragma: no cover - runtime dependency
            print(f"Failed to ingest {path}: {exc}")


if __name__ == "__main__":
    main()
