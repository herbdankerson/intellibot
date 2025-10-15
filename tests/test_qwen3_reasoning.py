"""Integration test documenting a Qwen3 reasoning call with SearxNG evidence."""

from __future__ import annotations

import os
import textwrap
from typing import Iterable, Optional

import httpx
import pytest


_QWEN_BASE = os.getenv("QWEN3_API_BASE", "http://127.0.0.1:8001")
_QWEN_MODEL = os.getenv("QWEN3_MODEL_ID")
_SEARX_BASE = os.getenv("SEARXNG_BASE_URL", "http://127.0.0.1:8085")


def _require_service(url: str, *, timeout: float = 5.0) -> httpx.Response:
    """Return a healthy response or skip the test when service is offline."""

    try:
        response = httpx.get(url, timeout=httpx.Timeout(timeout, connect=timeout / 2))
        response.raise_for_status()
        return response
    except Exception as exc:  # pragma: no cover - network guard
        pytest.skip(f"Service unavailable at {url}: {exc}")


def _first_model_id(registry: dict) -> Optional[str]:
    """Extract the first model identifier from a models payload."""

    for key in ("data", "models"):
        items = registry.get(key)
        if isinstance(items, Iterable):
            for entry in items:
                if isinstance(entry, dict):
                    candidate = entry.get("id") or entry.get("model") or entry.get("name")
                    if candidate:
                        return str(candidate)
    return None


def _collect_searx_evidence(query: str) -> list[tuple[int, str, str, str]]:
    """Return up to one evidence tuple of (index, title, url, snippet)."""

    search_url = f"{_SEARX_BASE.rstrip('/')}/search"
    try:
        response = httpx.get(
            search_url,
            params={"q": query, "format": "json", "language": "en", "safesearch": 1, "num": 5},
            timeout=httpx.Timeout(15.0, connect=5.0),
        )
        response.raise_for_status()
    except Exception as exc:  # pragma: no cover - network guard
        pytest.skip(f"SearxNG search failed: {exc}")
    payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        pytest.skip("SearxNG returned no JSON results")
    evidence: list[tuple[int, str, str, str]] = []
    for index, entry in enumerate(results, 1):
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        url = str(entry.get("url") or "").strip()
        snippet_raw = entry.get("content") or entry.get("snippet") or ""
        snippet = textwrap.shorten(str(snippet_raw).replace("\n", " ")[:400], width=160)
        if title and url and snippet:
            evidence.append((index, title, url, snippet))
        if len(evidence) >= 1:
            break
    if not evidence:
        pytest.skip("SearxNG did not yield usable evidence")
    return evidence


@pytest.mark.slow
def test_qwen3_reasoning_with_searx_sources() -> None:
    """Exercise Qwen3 reasoning mode using a sourced evidence prompt."""

    health = _require_service(f"{_QWEN_BASE.rstrip('/')}/health")
    assert health.json().get("status") in {"ok", "pass"}

    model_id = _QWEN_MODEL
    if not model_id:
        models_resp = _require_service(f"{_QWEN_BASE.rstrip('/')}/v1/models", timeout=10.0)
        model_id = _first_model_id(models_resp.json() if models_resp.headers.get("content-type", "").startswith("application/json") else {})
    if not model_id:
        pytest.skip("Qwen3 model registry did not return an identifier")

    query = "Summarize the key objectives of the U.S. CHIPS and Science Act for domestic semiconductor production and research."
    evidence_items = _collect_searx_evidence(query)
    evidence_lines = [
        f"[{idx}] {title} - {url}\n    {snippet}"
        for idx, title, url, snippet in evidence_items
    ]
    evidence_block = "\n".join(evidence_lines)

    system_prompt = (
        "You are an analytical policy assistant. Use the evidence snippet to answer in no more than three sentences. "
        "Think briefly (<60 tokens) before replying. Cite the source inline as [n] and finish with a 'Sources:' list containing the numbered URLs."
    )
    user_prompt = (
        f"Question: {query}\n\n"
        f"Evidence:\n{evidence_block}\n\n"
        "Instructions: Keep the final answer under 100 tokens."
    )

    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 256,
        "reasoning_format": "deepseek-legacy",
        "thinking_forced_open": True,
    }

    with httpx.Client(timeout=httpx.Timeout(150.0, connect=5.0)) as client:
        response = client.post(f"{_QWEN_BASE.rstrip('/')}/v1/chat/completions", json=payload)
    response.raise_for_status()
    body = response.json()

    choices = body.get("choices")
    assert isinstance(choices, list) and choices, "Qwen3 returned no choices"
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    assert isinstance(message, dict), "Primary choice message missing"

    reasoning = message.get("reasoning_content")
    assert isinstance(reasoning, str) and reasoning.strip(), "Reasoning content should be populated"

    content = message.get("content")
    assert isinstance(content, str) and "Sources:" in content, "Answer should include a Sources section"
    assert "[1]" in content, "Answer should reference the first evidence item"

    usage = body.get("usage", {})
    assert int(usage.get("completion_tokens", 0)) > 0
