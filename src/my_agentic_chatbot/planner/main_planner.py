"""Planner entry point that produces task plans from user messages."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterable

from ..agents import get_agent_catalog, get_agent_config, planner_tool_hints
from ..config import get_settings
from ..llm_calls.llm_client import LLMClient, LLMMessage
from ..run_logging import AgentRunLogger
from ..schemas import Plan
from ..workflows import policies

LOGGER = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
_PROMPT_CACHE: Dict[str, str] = {}


def plan_from_message(
    message: str,
    *,
    model: str | None = None,
    client: LLMClient | None = None,
    logger: AgentRunLogger | None = None,
) -> Plan:
    """Return a plan for the provided user message using the LiteLLM proxy."""

    normalized = message.strip()
    if not normalized:
        raise ValueError("Planner requires a non-empty message")

    settings = get_settings()
    agent_config = get_agent_config("planner")
    model_aliases = settings.model_aliases()
    model_key = model or agent_config.model
    target_model = model_aliases.get(model_key, model_key)

    owns_client = client is None
    planner_client = client or LLMClient(model_name=target_model)
    try:
        messages = _build_messages(normalized)
        if logger is not None:
            logger.log_event(
                "planner_request",
                {
                    "model": target_model,
                    "messages": [msg.as_dict() for msg in messages],
                },
            )
        if isinstance(planner_client, LLMClient):
            response = planner_client.chat(messages, agent_config=agent_config)
        else:  # test doubles may not accept agent_config keyword
            response = planner_client.chat(messages)
        if logger is not None:
            logger.log_event(
                "planner_response_raw",
                {"response": response},
            )
    finally:
        if owns_client:
            planner_client.close()

    payload = _parse_plan_response(response, normalized)
    normalized_payload = _apply_defaults(payload, normalized)
    plan = Plan.model_validate(normalized_payload)
    if logger is not None:
        logger.log_plan(plan, raw_response=response)
    return plan


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
    tool_hints = planner_tool_hints()
    agent_guidance = "Available agents/tools:\n" + "\n".join(tool_hints) if tool_hints else ""
    schema_prompt = (
        "Respond with a JSON object containing keys: goals, assumptions, info_needed, tasks, "
        "stop_conditions, acceptance_criteria. The tasks array must contain objects with the "
        "fields id, description, tool, budget_tokens, timeout_seconds, requires_approval."
    )
    system_content = (
        f"{system_prompt}\n\nRubrics:\n{rubric_prompt}\n\n{budget_guidance}\n{agent_guidance}\n{schema_prompt}"
    )
    user_content = (
        f"User request: {user_message}\n"
        "Ensure the plan is minimal and references tools available via MCP or custom agents."
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

    catalog = get_agent_catalog()
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError("Planner tasks must be objects")
        tool = str(task.get("tool", "")).strip()
        descriptor = catalog.get(tool)
        task.setdefault(
            "budget_tokens",
            descriptor.default_budget_tokens if descriptor else policies.DEFAULT_DB_BUDGET_TOKENS,
        )
        task.setdefault(
            "timeout_seconds",
            descriptor.default_timeout_seconds if descriptor else policies.DEFAULT_DB_TIMEOUT_SECONDS,
        )
        identifier_value = task.get("id", "")
        if isinstance(identifier_value, (int, float)):
            if isinstance(identifier_value, float) and identifier_value.is_integer():
                identifier = str(int(identifier_value))
            else:
                identifier = str(identifier_value)
        else:
            identifier = str(identifier_value).strip()
        if not identifier:
            identifier = f"{_default_prefix(tool)}-{index + 1}"
        task["id"] = identifier
        requires_approval = descriptor.requires_approval if descriptor else tool == "graph_search"
        task.setdefault("requires_approval", requires_approval)
        description = str(task.get("description", "")).strip()
        if not description:
            task["description"] = f"Run {tool or 'a tool'} for: {goal}"
        inputs = task.setdefault("inputs", {})
        if isinstance(inputs, dict):
            task["inputs"] = {str(key): value for key, value in inputs.items()}
        else:
            task["inputs"] = {}
        dependencies = task.setdefault("depends_on", [])
        if isinstance(dependencies, str):
            task["depends_on"] = [dependencies]
        elif isinstance(dependencies, list):
            normalized_dependencies = []
            for dep in dependencies:
                dep_id = str(dep).strip()
                if dep_id:
                    normalized_dependencies.append(dep_id)
            task["depends_on"] = normalized_dependencies
        else:
            task["depends_on"] = []
    return payload


def _default_prefix(tool: str) -> str:
    normalized = tool or "task"
    if normalized.startswith("db"):
        return "db"
    if normalized.startswith("graph"):
        return "graph"
    if normalized.startswith("web"):
        return "web"
    if normalized.startswith("agent"):
        return "agent"
    return "task"


__all__ = ["plan_from_message"]
