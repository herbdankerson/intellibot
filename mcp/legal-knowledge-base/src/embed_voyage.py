"""LiteLLM-backed embedding helpers.

All embedding calls route through the LiteLLM proxy so that provider keys can
rotate automatically (Voyage pool by default). The helpers keep the old module
name so the call sites stay unchanged.
"""

from __future__ import annotations

from typing import Iterable, List

import requests

from src.config import (
    LITELLM_API_KEY,
    LITELLM_BASE_URL,
    LITELLM_TIMEOUT,
)


class LiteLLMEmbeddingError(RuntimeError):
    """Raised when LiteLLM returns an unexpected payload."""


def _embeddings_endpoint() -> str:
    base = LITELLM_BASE_URL.rstrip("/")
    return f"{base}/embeddings"


def _post_embeddings(model: str, texts: Iterable[str]) -> List[List[float]]:
    payload = {"model": model, "input": list(texts)}
    headers = {"Content-Type": "application/json"}
    if LITELLM_API_KEY:
        headers["Authorization"] = f"Bearer {LITELLM_API_KEY}"

    response = requests.post(
        _embeddings_endpoint(),
        json=payload,
        headers=headers,
        timeout=LITELLM_TIMEOUT,
    )
    response.raise_for_status()
    body = response.json()
    data = body.get("data")
    if not isinstance(data, list):  # pragma: no cover - defensive fallback
        raise LiteLLMEmbeddingError(f"Malformed LiteLLM response: {body}")

    # LiteLLM mirrors the OpenAI schema: ensure results are returned in index order.
    sorted_rows = sorted(
        data,
        key=lambda item: item.get("index", 0),
    )
    embeddings: List[List[float]] = []
    for item in sorted_rows:
        vector = item.get("embedding")
        if not isinstance(vector, list):
            raise LiteLLMEmbeddingError(f"Missing embedding vector in: {item}")
        embeddings.append(vector)
    return embeddings


def embed_docs(texts: List[str], model: str) -> List[List[float]]:
    """Batch-embed one or more documents."""

    if not texts:
        return []
    return _post_embeddings(model, texts)


def embed_query(text: str, model: str) -> List[float]:
    """Embed a single query or prompt."""

    result = _post_embeddings(model, [text])
    if not result:
        raise LiteLLMEmbeddingError("LiteLLM returned an empty embedding list")
    return result[0]
