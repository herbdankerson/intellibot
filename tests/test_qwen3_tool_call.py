"""Ensure Qwen3 emits OpenAI-style tool calls when tools are provided."""

from __future__ import annotations

import json
import os

import httpx
import pytest


_QWEN_BASE = os.getenv("QWEN3_API_BASE", "http://127.0.0.1:8001")
_QWEN_MODEL = os.getenv("QWEN3_MODEL_ID")


@pytest.mark.slow
def test_qwen3_issues_tool_call() -> None:
    """Qwen3 should produce a tool call when given an appropriate tool spec."""

    health = httpx.get(f"{_QWEN_BASE.rstrip('/')}/health", timeout=10.0)
    health.raise_for_status()
    assert health.json().get("status") in {"ok", "pass"}

    model_id = _QWEN_MODEL
    if not model_id:
        models_resp = httpx.get(f"{_QWEN_BASE.rstrip('/')}/v1/models", timeout=10.0)
        models_resp.raise_for_status()
        registry = models_resp.json()
        if isinstance(registry, dict):
            data = registry.get("data") or registry.get("models") or []
            for entry in data:
                if isinstance(entry, dict):
                    candidate = entry.get("id") or entry.get("model") or entry.get("name")
                    if candidate:
                        model_id = str(candidate)
                        break
    if not model_id:
        pytest.skip("Qwen3 model id unavailable")

    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant. Use tools when they help."},
            {"role": "user", "content": "Use the searxngsearch tool to look up the CHIPS Act."},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "searxngsearch",
                    "description": "Perform a web search via SearXNG",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "language": {"type": "string", "default": "en"},
                        },
                        "required": ["query"],
                    },
                },
            }
        ],
        "tool_choice": "auto",
        "temperature": 0.0,
        "reasoning_format": "deepseek-legacy",
        "thinking_forced_open": True,
        "parse_tool_calls": True,
    }

    response = httpx.post(
        f"{_QWEN_BASE.rstrip('/')}/v1/chat/completions",
        json=payload,
        timeout=httpx.Timeout(180.0, connect=5.0),
    )
    response.raise_for_status()
    body = response.json()

    choices = body.get("choices")
    assert isinstance(choices, list) and choices, "Qwen3 returned no choices"
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    assert isinstance(message, dict), "Primary choice message missing"

    tool_calls = message.get("tool_calls")
    assert isinstance(tool_calls, list) and tool_calls, "No tool call produced"

    call = tool_calls[0]
    assert call.get("type") == "function"
    fn = call.get("function", {})
    assert fn.get("name") == "searxngsearch"
    args_raw = fn.get("arguments")
    assert isinstance(args_raw, str) and args_raw, "Tool call arguments missing"

    args = json.loads(args_raw)
    assert args.get("query") in {"CHIPS Act", "chips act"}
    assert args.get("language", "en") == "en"

    usage = body.get("usage", {})
    assert int(usage.get("completion_tokens", 0)) > 0
