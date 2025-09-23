"""Planner entry point that produces task plans from user messages."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterable

from ..config import get_settings
from ..llm_calls.llm_client import LLMClient, LLMMessage
from ..schemas import Plan
from ..workflows import policies

LOGGER = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
_PROMPT_CACHE: Dict[str, str] = {}

_TOOL_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "db_search": {
        "budget": policies.DEFAULT_DB_BUDGET_TOKENS,
        "timeout": policies.DEFAULT_DB_TIMEOUT_SECONDS,
        "prefix": "db",
    },
    "graph_search": {
        "budget": policies.DEFAULT_GRAPH_BUDGET_TOKENS,
        "timeout": policies.DEFAULT_GRAPH_TIMEOUT_SECONDS,
        "prefix": "graph",
    },
    "web_search": {
        "budget": policies.DEFAULT_WEB_BUDGET_TOKENS,
        "timeout": policies.DEFAULT_WEB_TIMEOUT_SECONDS,
        "prefix": "web",
    },
}


def plan_from_message(
    message: str,
    *,
    model: str | None = None,
    client: LLMClient | None = None,
) -> Plan:
    """Return a plan for the provided user message using the LiteLLM proxy."""

    normalized = message.strip()
    if not normalized:
        raise ValueError("Planner requires a non-empty message")

    settings = get_settings()
    model_aliases = settings.model_aliases()
    target_model = model_aliases.get(model or "planner", model or settings.planner_model)

    owns_client = client is None
    planner_client = client or LLMClient(model_name=target_model)
    try:
        response = planner_client.chat(_build_messages(normalized))
    finally:
        if owns_client:
            planner_client.close()

    payload = _parse_plan_response(response, normalized)
    normalized_payload = _apply_defaults(payload, normalized)
    return Plan.model_validate(normalized_payload)


def _build_messages(user_message: str) -> Iterable[LLMMessage]:
    system_prompt = _load_prompt("planner_system.md")
    rubric_prompt = _load_prompt("rubrics.md")
    budget_guidance = (
        "Database budget: {db_tokens} tokens / {db_timeout}s. "
        "Graph budget: {graph_tokens} tokens / {graph_timeout}s (requires approval). "
        "Web budget: {web_tokens} tokens / {web_timeout}s."
    ).format(
        db_tokens=policies.DEFAULT_DB_BUDGET_TOKENS,
        db_timeout=policies.DEFAULT_DB_TIMEOUT_SECONDS,
        graph_tokens=policies.DEFAULT_GRAPH_BUDGET_TOKENS,
        graph_timeout=policies.DEFAULT_GRAPH_TIMEOUT_SECONDS,
        web_tokens=policies.DEFAULT_WEB_BUDGET_TOKENS,
        web_timeout=policies.DEFAULT_WEB_TIMEOUT_SECONDS,
    )
    schema_prompt = (
        "Respond with a JSON object containing keys: goals, assumptions, info_needed, tasks, "
        "stop_conditions, acceptance_criteria. The tasks array must contain objects with the "
        "fields id, description, tool, budget_tokens, timeout_seconds, requires_approval."
    )
    system_content = (
        f"{system_prompt}\n\nRubrics:\n{rubric_prompt}\n\n{budget_guidance}\n{schema_prompt}"
    )
    user_content = (
        f"User request: {user_message}\n"
        "Ensure the plan is minimal and references tools available via MCP."
    )
    return [
        LLMMessage(role="system", content=system_content),
        LLMMessage(role="user", content=user_content),
    ]


def _load_prompt(filename: str) -> str:
    cached = _PROMPT_CACHE.get(filename)
    if cached is not None:
        return cached
    path = _PROMPT_DIR / filename
    text = path.read_text(encoding="utf-8").strip()
    _PROMPT_CACHE[filename] = text
    return text


def _parse_plan_response(response: str, fallback_goal: str) -> Dict[str, Any]:
    if not response:
        LOGGER.warning("Planner returned empty response")
        return {"goals": [fallback_goal], "tasks": []}
    try:
        return _extract_json_object(response)
    except ValueError as exc:
        LOGGER.error("Failed to parse planner response", exc_info=exc)
        raise


def _extract_json_object(raw: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError("Planner response does not contain valid JSON")
        parsed = json.loads(match.group())
    if not isinstance(parsed, dict):
        raise ValueError("Planner response JSON must be an object")
    return parsed


def _apply_defaults(payload: Dict[str, Any], goal: str) -> Dict[str, Any]:
    payload.setdefault("goals", [goal])
    payload.setdefault("assumptions", [])
    payload.setdefault("info_needed", [goal])
    payload.setdefault("stop_conditions", ["Acceptance criteria satisfied"])
    payload.setdefault(
        "acceptance_criteria",
        [
            "Answer references evidence item identifiers",
            "Unresolved assumptions are captured for follow-up",
        ],
    )

    tasks = payload.setdefault("tasks", [])
    if not isinstance(tasks, list):
        raise ValueError("Planner tasks must be a list")

    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError("Planner tasks must be objects")
        tool = str(task.get("tool", "")).strip()
        defaults = _TOOL_DEFAULTS.get(
            tool,
            {
                "budget": policies.DEFAULT_DB_BUDGET_TOKENS,
                "timeout": policies.DEFAULT_DB_TIMEOUT_SECONDS,
                "prefix": tool or "task",
            },
        )
        task.setdefault("budget_tokens", defaults["budget"])
        task.setdefault("timeout_seconds", defaults["timeout"])
        identifier = str(task.get("id", "")).strip()
        if not identifier:
            task["id"] = f"{defaults['prefix']}-{index + 1}"
        if tool == "graph_search":
            task.setdefault("requires_approval", True)
        else:
            task.setdefault("requires_approval", False)
        description = str(task.get("description", "")).strip()
        if not description:
            task["description"] = f"Run {tool or 'a tool'} for: {goal}"
    return payload


__all__ = ["plan_from_message"]
