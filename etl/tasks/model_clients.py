"""Gateway-backed client helpers for ingestion-time LLM operations."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import httpx
from functools import lru_cache

from src.my_agentic_chatbot.config import get_settings
from src.my_agentic_chatbot.llm_calls.llm_client import get_current_run_logger
from src.my_agentic_chatbot.runtime_config import ModelConfig, get_runtime_config
from src.freshbot.gateways import embed_code_texts

LOGGER = logging.getLogger(__name__)

GENERAL_EMBED_BATCH = 32
LEGAL_EMBED_BATCH = 32
CODE_EMBED_BATCH = 16
DEFAULT_RETRIES = 3
EMBED_TOKEN_LIMIT = 450  # keep ~12% under the TEI gte-large 512-token ceiling
EMBED_APPROX_CHARS_PER_TOKEN = 1  # enforce a strict char clamp so actual token counts stay < limit
DEFAULT_EMBED_DIMS = 1536



def _active_model(key: str):
    """Return the active model configuration for the supplied cfg.active key."""

    return get_runtime_config().active(key)


def _truncate_snippet(text: str, limit: int = 320) -> Tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[: limit - 1] + "…", True


def _estimate_token_count(
    text: str,
    *,
    approx_chars_per_token: int = EMBED_APPROX_CHARS_PER_TOKEN,
) -> int:
    if not text:
        return 0
    # Whitespace tokenisation gives a lower bound; char-based estimate keeps us conservative.
    whitespace_tokens = len(text.split())
    char_tokens = len(text) // max(1, approx_chars_per_token)
    return max(whitespace_tokens, char_tokens)


def _truncate_for_embedding(
    text: str,
    *,
    max_tokens: int = EMBED_TOKEN_LIMIT,
    approx_chars_per_token: int = EMBED_APPROX_CHARS_PER_TOKEN,
) -> Tuple[str, bool]:
    """Clamp text to the provider's maximum token allowance."""

    if not text:
        return "", False

    char_limit = max_tokens * approx_chars_per_token
    truncated = text
    truncated_flag = False
    if len(truncated) > char_limit:
        truncated = truncated[:char_limit]
        truncated_flag = True
        last_whitespace = truncated.rfind(" ")
        if last_whitespace >= int(char_limit * 0.6):
            truncated = truncated[:last_whitespace]

    # Iteratively tighten until the conservative token estimate sits below the cap.
    while _estimate_token_count(truncated, approx_chars_per_token=approx_chars_per_token) >= max_tokens and truncated:
        truncated_flag = True
        new_length = max(1, int(len(truncated) * 0.9))
        truncated = truncated[:new_length]
        last_whitespace = truncated.rfind(" ")
        if last_whitespace >= max(1, int(new_length * 0.6)):
            truncated = truncated[:last_whitespace]

    return truncated.rstrip(), truncated_flag


def _sanitize_embedding_inputs(
    inputs: Sequence[str],
    *,
    max_tokens: int = EMBED_TOKEN_LIMIT,
) -> Tuple[List[str], List[Dict[str, object]]]:
    sanitized: List[str] = []
    truncation_meta: List[Dict[str, object]] = []
    for idx, raw in enumerate(inputs):
        value = "" if raw is None else str(raw)
        trimmed, truncated = _truncate_for_embedding(value, max_tokens=max_tokens)
        sanitized.append(trimmed)
        if truncated:
            truncation_meta.append(
                {
                    "index": idx,
                    "original_chars": len(value),
                    "truncated_chars": len(trimmed),
                }
            )
    return sanitized, truncation_meta


def _normalise_vector_dimensions(
    *,
    vectors: Sequence[Sequence[float]],
    target_dims: Optional[int],
    model_name: str,
) -> List[List[float]]:
    if target_dims is None or target_dims <= 0:
        return [list(vector) for vector in vectors]

    normalised: List[List[float]] = []
    truncated_count = 0
    padded_count = 0
    samples: List[Dict[str, object]] = []
    for index, vector in enumerate(vectors):
        as_list = list(vector)
        current = len(as_list)
        if current == target_dims:
            normalised.append(as_list)
            continue
        if current > target_dims:
            truncated_count += 1
            if len(samples) < 3:
                samples.append(
                    {
                        "index": index,
                        "original_dims": current,
                        "target_dims": target_dims,
                        "action": "truncated",
                    }
                )
            normalised.append(as_list[:target_dims])
            continue
        padding = target_dims - current
        padded_count += 1
        if len(samples) < 3:
            samples.append(
                {
                    "index": index,
                    "original_dims": current,
                    "target_dims": target_dims,
                    "padding": padding,
                    "action": "padded",
                }
            )
        normalised.append(as_list + [0.0] * padding)
    if truncated_count or padded_count:
        LOGGER.warning(
            "embedding vectors adjusted",
            extra={
                "model": model_name,
                "target_dims": target_dims,
                "truncated": truncated_count,
                "padded": padded_count,
                "samples": samples,
            },
        )
    return normalised
def embedding(*, model: ModelConfig, input: Sequence[str]) -> List[List[float]]:
    """Dispatch embedding requests directly to the configured provider."""

    return _call_embedding_provider(model=model, inputs=[str(text) for text in input])


@dataclass(frozen=True)
class ClassificationResult:
    """Structured result returned by the domain classifier."""

    domain: str
    confidence: float
    source_labels: List[str]


def summarize_with_gemini(text: str, *, max_length: int = 600) -> str:
    """Summarize the supplied text using the Freshbot Gemini tool."""

    return _fresh_ingestion().summarize_document(text, max_length=max_length)


def summarize_chunks_with_gemini(texts: Sequence[str], *, max_length: int = 256) -> List[str]:
    """Summarize a sequence of chunks using the Freshbot Gemini tool."""

    return _fresh_ingestion().summarize_chunks(texts, max_length=max_length)


def classify_domain(text: str) -> ClassificationResult:
    """Classify the document domain (`legal`, `code`, `general`)."""

    payload = _fresh_ingestion().classify_document(text)
    domain = str(payload.get("domain", "general"))
    confidence = float(payload.get("confidence", 0.5) or 0.5)
    labels = payload.get("source_labels")
    source_labels = [str(label) for label in labels] if isinstance(labels, list) else []
    return ClassificationResult(domain=domain, confidence=confidence, source_labels=source_labels)


def embed_with_general(texts: Sequence[str]) -> List[List[float]]:
    """Create embeddings for general content using the local TEI backend."""

    model = _active_model("active_emb_general")
    return _batched_embedding_request(texts, model=model, batch_size=GENERAL_EMBED_BATCH)


def embed_with_legal(texts: Sequence[str]) -> List[List[float]]:
    """Create embeddings for legal content using the local TEI backend."""

    model = _active_model("active_emb_legal")
    return _batched_embedding_request(texts, model=model, batch_size=LEGAL_EMBED_BATCH)


def embed_with_code(texts: Sequence[str]) -> List[List[float]]:
    """Create embeddings for code content using the configured remote model."""

    model = _active_model("active_emb_code")
    return _batched_embedding_request(texts, model=model, batch_size=CODE_EMBED_BATCH)


def embed_with_gemini(texts: Sequence[str]) -> List[List[float]]:  # pragma: no cover - maintained for compatibility
    """Backward compatible wrapper that now delegates to the general encoder."""

    return embed_with_general(texts)


def embed_with_voyage(texts: Sequence[str], *, model: str) -> List[List[float]]:  # pragma: no cover - maintained for compatibility
    """Backward compatible wrapper that routes to specialised embedding helpers."""

    canonical = model.lower()
    if canonical in {"voyage-law-2", _active_model("active_emb_legal").identifier}:
        return embed_with_legal(texts)
    if canonical in {"voyage-code-3", _active_model("active_emb_code").identifier}:
        return embed_with_code(texts)
    fallback = ModelConfig(
        name=model,
        provider="liteLLM",
        identifier=model,
        uri_template=None,
        resolved_uri=None,
        dims=None,
        purpose="embedding",
        enabled=True,
        version=None,
        notes=None,
        config={},
    )
    return _batched_embedding_request(texts, model=fallback, batch_size=CODE_EMBED_BATCH)


def analyze_emotions(text: str) -> List[Dict[str, Any]]:
    """Run emotion and sentiment classifiers against the supplied text."""
    return _fresh_ingestion().detect_emotions(text)


# ---------------------------------------------------------------------------
# Internal helpers

def _batched_embedding_request(
    texts: Sequence[str],
    *,
    model: ModelConfig,
    batch_size: int,
) -> List[List[float]]:
    vectors: List[List[float]] = []
    model_identifier = model.identifier
    model_name = model.name
    for batch in _batched(list(texts), batch_size):
        last_error: Optional[Exception] = None
        for attempt in range(1, DEFAULT_RETRIES + 1):
            try:
                vectors.extend(embedding(model=model, input=batch))
                break
            except Exception as exc:  # pragma: no cover - exercised via mocks
                last_error = exc
                LOGGER.warning(
                    "Embedding request attempt %s failed for model %s (%s): %s",
                    attempt,
                    model_name,
                    model_identifier,
                    exc,
                )
        else:
            LOGGER.error(
                "Embedding request failed after %s attempts; returning zero vectors",
                DEFAULT_RETRIES,
                extra={"model": model_name, "identifier": model_identifier},
            )
            dims = model.dims or DEFAULT_EMBED_DIMS
            zero_vector = [0.0] * dims
            vectors.extend([list(zero_vector) for _ in batch])
    return vectors


def _call_embedding_provider(
    *, model: ModelConfig, inputs: Sequence[str]
) -> List[List[float]]:
    sanitized_inputs, truncation_meta = _sanitize_embedding_inputs(inputs)
    if not sanitized_inputs:
        return []

    provider = model.provider.lower()
    if provider == "litellm":
        vectors = _embedding_via_litellm(
            model=model,
            inputs=sanitized_inputs,
            truncation_meta=truncation_meta,
        )
        target_dims = model.dims
        return _normalise_vector_dimensions(
            vectors=vectors,
            target_dims=target_dims,
            model_name=model.name,
        )
    if provider in {"tei", "text-embeddings-inference"}:
        vectors = _embedding_via_tei(
            model=model,
            inputs=sanitized_inputs,
            truncation_meta=truncation_meta,
        )
        target_dims = model.dims
        return _normalise_vector_dimensions(
            vectors=vectors,
            target_dims=target_dims,
            model_name=model.name,
        )
    if provider in {"ollama", "gateway"}:
        gateway_alias = model.config.get("gateway_alias") or model.name
        vectors = embed_code_texts(sanitized_inputs, gateway_alias=gateway_alias)
        target_dims = model.dims
        return _normalise_vector_dimensions(
            vectors=vectors,
            target_dims=target_dims,
            model_name=model.name,
        )
    raise RuntimeError(
        f"Unsupported embedding provider '{model.provider}' for model '{model.name}'"
    )


def _build_preview(inputs: Sequence[str]) -> List[Dict[str, object]]:
    preview: List[Dict[str, object]] = []
    for idx, text in enumerate(inputs[:3]):
        snippet, truncated = _truncate_snippet(text)
        preview.append(
            {
                "index": idx,
                "length": len(text),
                "snippet": snippet,
                "truncated": truncated,
            }
        )
    return preview


def _log_embedding_event(
    *,
    logger,
    tool: str,
    payload: Dict[str, object],
    status: str,
) -> None:
    if logger is None:
        return
    logger.log_event("llm_call", payload, tool=tool, status=status)


def _embedding_via_litellm(
    *,
    model: ModelConfig,
    inputs: Sequence[str],
    truncation_meta: Sequence[Dict[str, object]],
) -> List[List[float]]:
    settings = get_settings()
    base_url = (model.resolved_uri or settings.litellm_base_url).rstrip("/")
    timeout = settings.litellm_timeout_seconds
    headers = dict(settings.lite_llm_headers())
    if settings.litellm_master_key and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer {settings.litellm_master_key}"
    logger = get_current_run_logger()
    log_payload: Dict[str, object] = {
        "endpoint": "embeddings",
        "model": model.identifier,
        "input_count": len(inputs),
        "input_preview": _build_preview(inputs),
    }
    if truncation_meta:
        log_payload["truncated_inputs"] = list(truncation_meta)
    start = perf_counter()
    with httpx.Client(base_url=base_url, timeout=timeout) as client:
        try:
            response = client.post(
                "/v1/embeddings",
                json={"model": model.identifier, "input": list(inputs)},
                headers=headers or None,
            )
            response.raise_for_status()
            data = response.json()
            vectors = _extract_litellm_vectors(data)
            if len(vectors) != len(inputs):
                raise RuntimeError("Embedding count mismatch")
            elapsed = round((perf_counter() - start) * 1000, 2)
            log_payload.update(
                {
                    "status_code": response.status_code,
                    "elapsed_ms": elapsed,
                    "vector_dims": len(vectors[0]) if vectors else 0,
                }
            )
            _log_embedding_event(logger=logger, tool="liteLLM", payload=log_payload, status="success")
            return vectors
        except Exception as exc:
            elapsed = round((perf_counter() - start) * 1000, 2)
            log_payload.update({"elapsed_ms": elapsed, "error": str(exc)})
            _log_embedding_event(logger=logger, tool="liteLLM", payload=log_payload, status="error")
            raise


def _embedding_via_tei(
    *,
    model: ModelConfig,
    inputs: Sequence[str],
    truncation_meta: Sequence[Dict[str, object]],
) -> List[List[float]]:
    base_url = (model.resolved_uri or "").rstrip("/")
    if not base_url:
        raise RuntimeError(f"Model '{model.name}' missing TEI endpoint configuration")
    settings = get_settings()
    logger = get_current_run_logger()
    log_payload: Dict[str, object] = {
        "endpoint": f"{base_url}/embed",
        "model": model.identifier,
        "input_count": len(inputs),
        "input_preview": _build_preview(inputs),
    }
    if truncation_meta:
        log_payload["truncated_inputs"] = list(truncation_meta)
    start = perf_counter()
    with httpx.Client(base_url=base_url, timeout=settings.litellm_timeout_seconds) as client:
        try:
            response = client.post(
                "/embed",
                json={"inputs": list(inputs)},
            )
            response.raise_for_status()
            data = response.json()
            vectors = _extract_tei_vectors(data)
            if len(vectors) != len(inputs):
                raise RuntimeError("Embedding count mismatch")
            elapsed = round((perf_counter() - start) * 1000, 2)
            log_payload.update(
                {
                    "status_code": response.status_code,
                    "elapsed_ms": elapsed,
                    "vector_dims": len(vectors[0]) if vectors else 0,
                }
            )
            _log_embedding_event(logger=logger, tool="tei", payload=log_payload, status="success")
            return vectors
        except Exception as exc:
            elapsed = round((perf_counter() - start) * 1000, 2)
            log_payload.update({"elapsed_ms": elapsed, "error": str(exc)})
            _log_embedding_event(logger=logger, tool="tei", payload=log_payload, status="error")
            raise


def _extract_litellm_vectors(payload: Dict[str, object]) -> List[List[float]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise RuntimeError("LiteLLM embedding response missing data array")
    vectors: List[List[float]] = []
    for item in data:
        if not isinstance(item, dict) or "embedding" not in item:
            raise RuntimeError("LiteLLM embedding item missing embedding field")
        vector = item.get("embedding")
        if not isinstance(vector, list):
            raise RuntimeError("LiteLLM embedding vector was not a list")
        vectors.append([float(value) for value in vector])
    return vectors


def _extract_tei_vectors(payload: object) -> List[List[float]]:
    if isinstance(payload, dict):
        if "embeddings" in payload:
            payload = payload["embeddings"]
        elif "data" in payload:
            payload = payload["data"]
    if not isinstance(payload, list):
        raise RuntimeError("TEI embedding response missing embeddings list")
    vectors: List[List[float]] = []
    for entry in payload:
        if not isinstance(entry, list):
            raise RuntimeError("TEI embedding vector was not a list")
        vectors.append([float(value) for value in entry])
    return vectors


def _batched(items: List[str], size: int) -> Iterator[List[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


__all__ = [
    "ClassificationResult",
    "classify_domain",
    "embed_with_general",
    "embed_with_legal",
    "embed_with_code",
    "embed_with_gemini",
    "embed_with_voyage",
    "summarize_chunks_with_gemini",
    "summarize_with_gemini",
]
@lru_cache(maxsize=1)
def _fresh_ingestion():
    """Lazy loader to avoid circular imports during container bootstrap."""

    from src.freshbot.pipeline import ingestion as fresh_ingestion  # type: ignore

    return fresh_ingestion
