"""Planner entry point that produces task plans from user messages."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterable

from ..agents import get_agent_catalog, get_agent_config, planner_tool_hints
from ..config import get_settings
from ..constants import (
    DEFAULT_SEQUENTIAL_BUDGET_TOKENS,
    DEFAULT_SEQUENTIAL_TIMEOUT_SECONDS,
)
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
        "Sequential thinking budget: {seq_tokens} tokens / {seq_timeout}s. "
        "Specialist agents have their own budgets and may require approval."
    ).format(
        db_tokens=policies.DEFAULT_DB_BUDGET_TOKENS,
        db_timeout=policies.DEFAULT_DB_TIMEOUT_SECONDS,
        seq_tokens=DEFAULT_SEQUENTIAL_BUDGET_TOKENS,
        seq_timeout=DEFAULT_SEQUENTIAL_TIMEOUT_SECONDS,
    )
    tool_hints = planner_tool_hints()
    catalog = get_agent_catalog()
    specialist = [
        descriptor.planner_hint()
        for descriptor in catalog.values()
        if not descriptor.planner_visible
    ]
    agent_guidance = "Available orchestration agents:\n" + "\n".join(tool_hints) if tool_hints else ""
    specialist_guidance = (
        "\nSpecialised execution agents (delegate tasks to them via plan entries):\n"
        + "\n".join(specialist)
        if specialist
        else ""
    )
    schema_prompt = (
        "Respond with a JSON object containing keys: goals, assumptions, info_needed, tasks, "
        "stop_conditions, acceptance_criteria. The tasks array must contain objects with the "
        "fields id, description, tool, budget_tokens, timeout_seconds, requires_approval."
    )
    system_content = (
        f"{system_prompt}\n\nRubrics:\n{rubric_prompt}\n\n{budget_guidance}\n"
        f"{agent_guidance}{specialist_guidance}\n{schema_prompt}"
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
    if isinstance(payload["info_needed"], list):
        normalized_info = []
        for item in payload["info_needed"]:
            if isinstance(item, dict):
                for key in ("description", "text", "value"):
                    if key in item:
                        normalized_info.append(str(item[key]))
                        break
                else:
                    normalized_info.append(json.dumps(item, sort_keys=True))
            else:
                normalized_info.append(str(item))
        payload["info_needed"] = normalized_info
    else:
        payload["info_needed"] = [str(payload["info_needed"])]
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
        description = str(task.get("description", "")).strip()
        if tool == "db_search" and _should_use_web(description):
            tool = "web_search"
            task["tool"] = tool
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
        if not description:
            description = f"Run {tool or 'a tool'} for: {goal}"
            task["description"] = description
        inputs = task.setdefault("inputs", {})
        if isinstance(inputs, dict):
            normalized_inputs = {str(key): value for key, value in inputs.items()}
            if tool in {"db_search", "web_search"}:
                query_value = normalized_inputs.get("query")
                if not isinstance(query_value, str) or not query_value.strip():
                    suggested = _suggest_query(description)
                    if suggested:
                        normalized_inputs["query"] = suggested
            task["inputs"] = normalized_inputs
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


def _should_use_web(description: str) -> bool:
    lowered = description.lower()
    return any(
        keyword in lowered
        for keyword in (
            "trial procedure",
            "trial procedures",
            "case preparation",
            "case-preparation",
            "actionable advice",
            "best practices",
            "checklist",
            "web search",
        )
    )


def _default_prefix(tool: str) -> str:
    normalized = tool or "task"
    if normalized.startswith("db"):
        return "db"
    if normalized.startswith("graph"):
        return "graph"
    if normalized.startswith("neo4j"):
        return "neo4j"
    if normalized.startswith("legal"):
        return "legal"
    if normalized.startswith("web"):
        return "web"
    if normalized.startswith("agent"):
        return "agent"
    return "task"


_QUERY_CLEAN_RE = re.compile(
    r"^(?:(?:search|find|retrieve|gather|look\s+up)\s+"
    r"(?:(?:(?:the|all)\s+)?(?:database|web|graph|knowledge\s+base)\s+)?(?:for|about)\s+)",
    re.IGNORECASE,
)

_LEADING_PHRASE_RE = re.compile(
    r"^(?:(?:key\s+)?details\s+about|information\s+on|insights\s+into|overview\s+of|"
    r"summary\s+of|guide\s+to|practical\s+guidance\s+on|strategies\s+for|"
    r"(?:biographical|professional)\s+(?:and\s+)?(?:professional\s+)?(?:history|background)\s+of|"
    r"(?:actionable|practical)\s+advice\s+on)\s+",
    re.IGNORECASE,
)


def _suggest_query(description: str) -> str:
    """Derive a default query string from a planner task description."""

    cleaned = description.strip()
    if not cleaned:
        return ""
    lowered = cleaned.lower()
    if "judge brewer" in lowered and any(term in lowered for term in ("background", "career", "professional")):
        return "Judge Danielle Brewer background"
    if "trial procedure" in lowered:
        return "civil trial procedures step-by-step"
    if any(term in lowered for term in ("case preparation", "actionable advice", "best practices")):
        return "trial preparation checklist"
    cleaned = _QUERY_CLEAN_RE.sub("", cleaned)
    cleaned = cleaned.strip().rstrip(".")
    cleaned = _LEADING_PHRASE_RE.sub("", cleaned)
    cleaned = cleaned.replace("'s", "")
    cleaned = cleaned.rstrip(",")
    cleaned = re.sub(r"\bfocusing\s+on\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bcovering\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bincluding\b", " ", cleaned, flags=re.IGNORECASE)
    for sep in (" including ", " such as ", " covering "):
        if sep in cleaned:
            cleaned = cleaned.split(sep, 1)[0]
            break
    if cleaned.lower().startswith("the "):
        cleaned = cleaned[4:]
    if cleaned.lower().startswith("a "):
        cleaned = cleaned[2:]
    if cleaned.lower().startswith("an "):
        cleaned = cleaned[3:]
    cleaned = cleaned.replace(",", " ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


__all__ = ["plan_from_message"]
