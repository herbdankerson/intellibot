"""LiteLLM-backed client helpers for Gemini and Voyage models."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from time import perf_counter
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import httpx

from src.my_agentic_chatbot.config import get_settings
from src.my_agentic_chatbot.llm_calls.llm_client import (
    LLMClient,
    LLMMessage,
    get_current_run_logger,
)

LOGGER = logging.getLogger(__name__)

SUMMARIZER_MODEL = "cheap-worker"
CLASSIFIER_MODEL = "cheap-worker"
GEMINI_EMBED_MODEL = "gemini/embedding-001"
VOYAGE_LAW_MODEL = "voyage-law-2"
VOYAGE_CODE_MODEL = "voyage-code-3"

GEMINI_SUMMARY_BATCH = 8
GEMINI_EMBED_BATCH = 8
VOYAGE_EMBED_BATCH = 16
DEFAULT_RETRIES = 3


def _truncate_snippet(text: str, limit: int = 320) -> Tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[: limit - 1] + "…", True


def completion(
    *,
    model: str,
    messages: List[Dict[str, str]],
    temperature: float = 0.0,
    max_output_tokens: Optional[int] = None,
) -> Dict[str, object]:
    """LiteLLM-compatible chat completion helper."""

    llm_messages = [
        LLMMessage(role=entry["role"], content=entry["content"])
        for entry in messages
    ]
    client = LLMClient(model_name=model, temperature=temperature)
    kwargs: Dict[str, object] = {}
    if max_output_tokens is not None:
        kwargs["max_tokens"] = max_output_tokens
    try:
        content = client.chat(llm_messages, **kwargs)
    finally:
        client.close()
    payload_content = "" if content is None else str(content)
    return {"choices": [{"message": {"content": payload_content}}]}


def embedding(*, model: str, input: Sequence[str]) -> Dict[str, object]:
    """LiteLLM embedding helper that proxies to the configured router."""

    settings = get_settings()
    base_url = settings.litellm_base_url
    timeout = settings.litellm_timeout_seconds
    headers = dict(settings.lite_llm_headers())
    if settings.litellm_master_key and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer {settings.litellm_master_key}"
    inputs = [str(text) for text in input]
    preview = []
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
    logger = get_current_run_logger()
    log_payload = {
        "endpoint": "embeddings",
        "model": model,
        "input_count": len(inputs),
        "input_preview": preview,
    }
    start = perf_counter()
    with httpx.Client(base_url=base_url, timeout=timeout) as client:
        try:
            response = client.post(
                "/v1/embeddings",
                json={"model": model, "input": inputs},
                headers=headers or None,
            )
            response.raise_for_status()
            data = response.json()
            if logger is not None:
                vectors = data.get("data") if isinstance(data, dict) else None
                dims = 0
                if vectors and isinstance(vectors, list) and vectors and isinstance(vectors[0], dict):
                    embedding = vectors[0].get("embedding")
                    if isinstance(embedding, list):
                        dims = len(embedding)
                log_payload.update(
                    {
                        "status_code": response.status_code,
                        "elapsed_ms": round((perf_counter() - start) * 1000, 2),
                        "vector_dims": dims,
                    }
                )
                logger.log_event(
                    "llm_call",
                    log_payload,
                    tool="liteLLM",
                    status="success",
                )
            return data
        except Exception as exc:
            if logger is not None:
                log_payload.update(
                    {
                        "elapsed_ms": round((perf_counter() - start) * 1000, 2),
                        "error": str(exc),
                    }
                )
                logger.log_event(
                    "llm_call",
                    log_payload,
                    tool="liteLLM",
                    status="error",
                )
            raise


@dataclass(frozen=True)
class ClassificationResult:
    """Structured result returned by the domain classifier."""

    domain: str
    confidence: float
    source_labels: List[str]


def summarize_with_gemini(text: str, *, max_length: int = 600) -> str:
    """Summarize the supplied text using Gemini 2.5 Flash via LiteLLM."""

    payload = {
        "role": "user",
        "max_length": max_length,
        "content": text,
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You are a senior analyst producing factual document summaries. "
                "Write in complete sentences, stay under the requested character cap, "
                "and avoid prefacing the summary."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(payload),
        },
    ]
    response_text = _invoke_chat(
        messages,
        model=SUMMARIZER_MODEL,
        temperature=0.1,
        max_output_tokens=max_length,
    )
    summary = response_text.strip()
    if len(summary) > max_length:
        summary = summary[: max_length - 3].rstrip() + "..."
    return summary


def summarize_chunks_with_gemini(texts: Sequence[str], *, max_length: int = 256) -> List[str]:
    """Summarize a sequence of chunks using batched Gemini calls."""

    if not texts:
        return []

    summaries: List[str] = []
    for batch in _batched(list(texts), GEMINI_SUMMARY_BATCH):
        payload = {
            "max_length": max_length,
            "items": [
                {
                    "index": index,
                    "text": text,
                }
                for index, text in enumerate(batch)
            ],
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "You summarise passages for retrieval. For each input item respond with a "
                    "concise summary under the specified character limit. Return a JSON array "
                    "of strings ordered to match the provided indexes."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(payload),
            },
        ]
        response_text = _invoke_chat(
            messages,
            model=SUMMARIZER_MODEL,
            temperature=0.1,
            max_output_tokens=max_length,
        )
        summaries.extend(_parse_summary_array(response_text, batch, max_length))
    return summaries


def classify_domain(text: str) -> ClassificationResult:
    """Classify the document domain (`legal`, `code`, `general`)."""

    messages = [
        {
            "role": "system",
            "content": (
                "You are a classifier labelling content for a retrieval pipeline. "
                "Choose the primary domain from ['legal', 'code', 'general']. "
                "Respond with compact JSON containing domain, confidence (0-1), "
                "and any applicable source_labels (e.g. ['document'], ['website'])."
            ),
        },
        {
            "role": "user",
            "content": json.dumps({"content": text[:6000]}),
        },
    ]
    response_text = _invoke_chat(
        messages,
        model=CLASSIFIER_MODEL,
        temperature=0.0,
        max_output_tokens=256,
    )
    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError as exc:
        LOGGER.warning("Classifier returned non-JSON payload: %s", response_text)
        raise RuntimeError("Gemini classifier returned invalid JSON") from exc

    domain = str(payload.get("domain", "general")).lower()
    if domain not in {"legal", "code", "general"}:
        LOGGER.warning("Classifier returned unexpected domain '%s'", domain)
        domain = "general"
    confidence = float(payload.get("confidence", 0.5))
    source_labels_raw = payload.get("source_labels")
    if isinstance(source_labels_raw, list):
        source_labels = [str(label) for label in source_labels_raw]
    else:
        source_labels = []
    return ClassificationResult(domain=domain, confidence=confidence, source_labels=source_labels)


def embed_with_gemini(texts: Sequence[str]) -> List[List[float]]:
    """Create embeddings with Gemini `embedding-001` via LiteLLM."""

    return _batched_embedding_request(texts, GEMINI_EMBED_MODEL, GEMINI_EMBED_BATCH)


def embed_with_voyage(texts: Sequence[str], *, model: str) -> List[List[float]]:
    """Create embeddings using Voyage models via LiteLLM."""

    return _batched_embedding_request(texts, model, VOYAGE_EMBED_BATCH)


# ---------------------------------------------------------------------------
# Internal helpers


def _invoke_chat(
    messages: List[Dict[str, str]],
    *,
    model: str,
    temperature: float,
    max_output_tokens: Optional[int] = None,
) -> str:
    last_error: Optional[Exception] = None
    for attempt in range(1, DEFAULT_RETRIES + 1):
        try:
            response = completion(
                model=model,
                messages=messages,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
            )
            choices = response.get("choices") if isinstance(response, dict) else None
            if not choices:
                return ""
            message = choices[0].get("message") if isinstance(choices[0], dict) else {}
            content = "" if message is None else message.get("content", "")
            return "" if content is None else str(content)
        except Exception as exc:  # pragma: no cover - exercised via mocks
            last_error = exc
            LOGGER.warning(
                "LiteLLM chat attempt %s failed for model %s: %s",
                attempt,
                model,
                exc,
            )
        finally:
            try:
                client.close()
            except Exception:  # pragma: no cover - defensive
                pass
    raise RuntimeError("Gemini completion failed") from last_error


def _batched_embedding_request(
    texts: Sequence[str],
    model: str,
    batch_size: int,
) -> List[List[float]]:
    vectors: List[List[float]] = []
    for batch in _batched(list(texts), batch_size):
        last_error: Optional[Exception] = None
        for attempt in range(1, DEFAULT_RETRIES + 1):
            try:
                response = embedding(model=model, input=batch)
                data = response.get("data") if isinstance(response, dict) else []
                if len(data) != len(batch):
                    raise RuntimeError("Embedding count mismatch")
                vectors.extend([list(item.get("embedding", [])) for item in data])
                break
            except Exception as exc:  # pragma: no cover - exercised via mocks
                last_error = exc
                LOGGER.warning(
                    "Embedding request attempt %s failed for model %s: %s",
                    attempt,
                    model,
                    exc,
                )
        else:
            raise RuntimeError("Embedding request failed") from last_error
    return vectors


def _batched(items: List[str], size: int) -> Iterator[List[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _parse_summary_array(
    response: str,
    fallbacks: Sequence[str],
    max_length: int,
) -> List[str]:
    try:
        data = json.loads(response)
    except json.JSONDecodeError:
        LOGGER.warning("Failed to parse summary array, falling back to truncation")
        return [summarize_with_gemini(text, max_length=max_length) for text in fallbacks]

    if isinstance(data, dict) and "summaries" in data:
        data = data.get("summaries")

    if not isinstance(data, list):
        LOGGER.warning("Summary payload was not a list: %s", data)
        return [summarize_with_gemini(text, max_length=max_length) for text in fallbacks]

    output: List[str] = []
    for idx, (candidate, fallback) in enumerate(zip(data, fallbacks)):
        summary = str(candidate) if candidate is not None else ""
        summary = summary.strip()
        if not summary:
            summary = summarize_with_gemini(fallback, max_length=max_length)
        elif len(summary) > max_length:
            summary = summary[: max_length - 3].rstrip() + "..."
        output.append(summary)

    # If model returned fewer summaries than requested, fill remaining slots.
    while len(output) < len(fallbacks):
        fallback = fallbacks[len(output)]
        output.append(summarize_with_gemini(fallback, max_length=max_length))

    return output


__all__ = [
    "ClassificationResult",
    "classify_domain",
    "embed_with_gemini",
    "embed_with_voyage",
    "summarize_chunks_with_gemini",
    "summarize_with_gemini",
]
