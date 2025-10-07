"""Generate embeddings for knowledge base chunks via LiteLLM."""

from __future__ import annotations
import argparse
from pathlib import Path
import sys
from typing import Any, Iterable, List

import httpx
from sqlalchemy import text

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.append(str(BASE_DIR))

from src.my_agentic_chatbot.config import get_settings
from src.my_agentic_chatbot.storage.db import get_engine

SPACE_CONFIG: dict[str, dict[str, Any]] = {
    "emb-general": {
        "model": "thenlper/gte-large",
        "provider": "local-tei",
        "dims": 1024,
        "distance_metric": "cosine",
    },
    "emb-law": {
        "model": "nlpaueb/legal-bert-base-uncased",
        "provider": "local-tei",
        "dims": 768,
        "distance_metric": "cosine",
    },
    "emb-code": {
        "model": "gemini/embedding-001",
        "provider": "google-gemini",
        "dims": 768,
        "distance_metric": "cosine",
    },
}

EMBEDDING_SPACES = tuple(SPACE_CONFIG.keys())


def ensure_space(space_name: str) -> int:
    config = SPACE_CONFIG[space_name]
    engine = get_engine()
    with engine.begin() as connection:
        result = connection.execute(
            text(
                """
                INSERT INTO kb.embedding_spaces (name, model, provider, dims, distance_metric)
                VALUES (:name, :model, :provider, :dims, :distance_metric)
                ON CONFLICT (name) DO UPDATE
                SET model = EXCLUDED.model,
                    provider = EXCLUDED.provider,
                    dims = EXCLUDED.dims,
                    distance_metric = EXCLUDED.distance_metric
                RETURNING id
                """
            ),
            {
                "name": space_name,
                "model": config["model"],
                "provider": config["provider"],
                "dims": config["dims"],
                "distance_metric": config["distance_metric"],
            },
        )
        return result.scalar_one()


def fetch_pending_chunks(space_id: int, limit: int) -> List[dict[str, Any]]:
    engine = get_engine()
    with engine.connect() as connection:
        result = connection.execute(
            text(
                """
                SELECT c.id AS chunk_id, c.text
                FROM kb.chunks c
                WHERE NOT EXISTS (
                    SELECT 1 FROM kb.chunk_embeddings e
                    WHERE e.chunk_id = c.id AND e.space_id = :space_id
                )
                ORDER BY c.created_at
                LIMIT :limit
                """
            ),
            {"space_id": space_id, "limit": limit},
        )
        return [dict(row._mapping) for row in result]


def embed_texts(client: httpx.Client, model: str, texts: Iterable[str]) -> List[List[float]]:
    payload = {"model": model, "input": list(texts)}
    response = client.post("/v1/embeddings", json=payload)
    response.raise_for_status()
    data = response.json()
    return [item["embedding"] for item in data.get("data", [])]


def to_vector_literal(values: Iterable[float]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in values) + "]"


def insert_embeddings(space_id: int, rows: List[dict[str, Any]], vectors: List[List[float]]) -> None:
    engine = get_engine()
    with engine.begin() as connection:
        for row, vector in zip(rows, vectors):
            connection.execute(
                text(
                    """
                    INSERT INTO kb.chunk_embeddings (chunk_id, space_id, embedding)
                    VALUES (:chunk_id, :space_id, CAST(:embedding AS vector))
                    ON CONFLICT (chunk_id, space_id) DO UPDATE
                    SET embedding = EXCLUDED.embedding,
                        created_at = NOW()
                    """
                ),
                {
                    "chunk_id": row["chunk_id"],
                    "space_id": space_id,
                    "embedding": to_vector_literal(vector),
                },
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Embed knowledge base chunks")
    parser.add_argument("--space", choices=EMBEDDING_SPACES, default="emb-general")
    parser.add_argument("--batch-size", type=int, default=32)
    args = parser.parse_args()

    space_id = ensure_space(args.space)
    settings = get_settings()
    total = 0
    with httpx.Client(
        base_url=settings.litellm_base_url,
        headers=settings.lite_llm_headers(),
        timeout=settings.litellm_timeout_seconds,
    ) as client:
        while True:
            rows = fetch_pending_chunks(space_id, args.batch_size)
            if not rows:
                break
            vectors = embed_texts(client, args.space, [row["text"] for row in rows])
            if len(vectors) != len(rows):
                raise RuntimeError("Embedding response count does not match request")
            insert_embeddings(space_id, rows, vectors)
            total += len(rows)
            print(f"Embedded {len(rows)} chunks; total={total}")

    if total == 0:
        print(f"No new chunks to embed for space '{args.space}'.")
    else:
        print(f"Embedded {total} chunks into space '{args.space}'.")


if __name__ == "__main__":
    main()
