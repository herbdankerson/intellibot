"""Planner entry points that produce structured plans from user messages."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

from ..agents import AgentConfig, get_agent_catalog, get_agent_config, planner_tool_hints
from ..constants import (
    DEFAULT_SEQUENTIAL_BUDGET_TOKENS,
    DEFAULT_SEQUENTIAL_TIMEOUT_SECONDS,
)
from ..llm_calls.llm_client import (
    LLMClient,
    LLMMessage,
    push_run_logger,
    reset_run_logger,
)
from ..run_logging import AgentRunLogger
from ..schemas import Finding, OpenQuestion, Plan, PlanTask, Requirement
from ..util.text import extract_subject_and_location, squeeze_whitespace
from ..workflows import policies

LOGGER = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
_PROMPT_CACHE: Dict[str, str] = {}

_JSON_ONLY_SUFFIX = (
    "Return ONLY the JSON object. Do not wrap in Markdown. Use strict JSON syntax"
    " (no trailing commas, comments, or additional narration)."
)


def plan_from_message(
    message: str,
    *,
    prior_findings: Sequence[Finding] | None = None,
    prior_open_questions: Sequence[OpenQuestion] | None = None,
    model: str | None = None,
    client: LLMClient | None = None,
    logger: AgentRunLogger | None = None,
) -> Plan:
    """Return a structured plan for the provided user message using the planner model."""

    normalized = message.strip()
    if not normalized:
        raise ValueError("Planner requires a non-empty message")

    agent_config = get_agent_config("planner")
    token = push_run_logger(logger) if logger is not None else None
    planner_client, target_model = _resolve_client(
        model=model,
        client=client,
        agent_config=agent_config,
    )
    owns_client = client is None
    try:
        base_messages = _build_initial_messages(
            normalized,
            prior_findings=prior_findings,
            prior_open_questions=prior_open_questions,
            system_prompt=agent_config.system_prompt or _load_prompt("planner_system.md"),
        )
        attempt_messages = list(base_messages)
        response = ""
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            if logger is not None:
                logger.log_event(
                    "planner_request",
                    {
                        "model": target_model,
                        "messages": [msg.as_dict() for msg in attempt_messages],
                        "attempt": attempt,
                    },
                )
            response = _chat(planner_client, attempt_messages)
            if logger is not None:
                logger.log_event(
                    "planner_response_raw",
                    {"response": response, "attempt": attempt},
                )
            try:
                payload = _parse_plan_response(response, fallback_problem_spec=normalized)
                break
            except ValueError:
                if attempt == max_attempts:
                    raise
                LOGGER.warning(
                    "Planner attempt %s produced invalid JSON; retrying. Snippet: %s",
                    attempt,
                    response[:400],
                )
                attempt_messages = list(base_messages)
                attempt_messages.append(LLMMessage(role="assistant", content=response))
                attempt_messages.append(
                    LLMMessage(
                        role="user",
                        content=(
                            "The previous reply was not valid JSON. Respond again with"
                            " JSON that strictly matches the planner schema."
                        ),
                    )
                )
        else:  # pragma: no cover - defensive
            payload = _parse_plan_response(response, fallback_problem_spec=normalized)
    finally:
        if owns_client:
            planner_client.close()
        if token is not None:
            reset_run_logger(token)

    normalized_payload = _normalize_payload(payload, normalized)
    plan = Plan.model_validate(normalized_payload)
    if logger is not None:
        logger.log_plan(plan, raw_response=response)
    return plan


def revise_plan_with_evidence(
    plan: Plan,
    *,
    user_message: str,
    new_findings: Sequence[Finding],
    new_open_questions: Sequence[OpenQuestion] | None = None,
    model: str | None = None,
    client: LLMClient | None = None,
    logger: AgentRunLogger | None = None,
) -> Plan:
    """Ask the planner to revise a plan after reviewing new findings."""

    agent_config = get_agent_config("planner")
    planner_client, target_model = _resolve_client(
        model=model,
        client=client,
        agent_config=agent_config,
    )
    owns_client = client is None
    token = push_run_logger(logger) if logger is not None else None
    try:
        messages = _build_revision_messages(
            plan=plan,
            user_message=user_message,
            new_findings=new_findings,
            new_open_questions=new_open_questions,
            system_prompt=agent_config.system_prompt or _load_prompt("planner_system.md"),
        )
        if logger is not None:
            logger.log_event(
                "planner_revision_request",
                {
                    "model": target_model,
                    "messages": [msg.as_dict() for msg in messages],
                },
            )
        response = _chat(planner_client, messages)
        if logger is not None:
            logger.log_event("planner_revision_response_raw", {"response": response})
    finally:
        if owns_client:
            planner_client.close()
        if token is not None:
            reset_run_logger(token)

    payload = _parse_plan_response(response, fallback_problem_spec=plan.problem_spec)
    normalized_payload = _normalize_payload(payload, plan.problem_spec)
    revised_plan = Plan.model_validate(normalized_payload)
    if logger is not None:
        logger.log_plan(revised_plan, raw_response=response)
    return revised_plan


def _resolve_client(
    *, model: str | None, client: LLMClient | None, agent_config: AgentConfig
) -> tuple[LLMClient, str]:
    target_model = model or agent_config.model
    if client is not None:
        return client, target_model
    return LLMClient(model_name=target_model), target_model


def _chat(client: LLMClient, messages: Iterable[LLMMessage]) -> str:
    if isinstance(client, LLMClient):
        agent_config = get_agent_config("planner")
        return client.chat(messages, agent_config=agent_config)
    return client.chat(messages)


def _build_initial_messages(
    user_message: str,
    *,
    prior_findings: Sequence[Finding] | None,
    prior_open_questions: Sequence[OpenQuestion] | None,
    system_prompt: str,
) -> List[LLMMessage]:
    rubric_prompt = _load_prompt("rubrics.md")
    budget_guidance = (
        "Database budget: {db_tokens} tokens / {db_timeout}s. "
        "Sequential thinking budget: {seq_tokens} tokens / {seq_timeout}s. "
        "Specialist agents provide their own budgets."
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
    tool_section = "Available orchestration tools:\n" + "\n".join(tool_hints) if tool_hints else ""
    specialist_section = (
        "\nSpecialised execution agents (delegate via plan tasks):\n"
        + "\n".join(specialist)
        if specialist
        else ""
    )

    schema_prompt = _schema_prompt()
    system_content = (
        f"{system_prompt}\n\nRubrics:\n{rubric_prompt}\n\n{budget_guidance}\n"
        f"{tool_section}{specialist_section}\n\n{schema_prompt}\n{_JSON_ONLY_SUFFIX}"
    )

    user_sections = [f"User request:\n{user_message.strip()}"]
    if prior_findings:
        serialized_findings = json.dumps([
            finding.model_dump() for finding in prior_findings
        ], ensure_ascii=False, indent=2)
        user_sections.append(
            "Existing findings (use to avoid redundant work):\n" + serialized_findings
        )
    if prior_open_questions:
        serialized_questions = json.dumps(
            [question.model_dump() for question in prior_open_questions],
            ensure_ascii=False,
            indent=2,
        )
        user_sections.append(
            "Outstanding questions to resolve if possible:\n" + serialized_questions
        )
    user_sections.append(
        "Return a plan JSON that follows the schema. Include at least one requirement."
    )
    user_content = "\n\n".join(user_sections)
    return [
        LLMMessage(role="system", content=system_content),
        LLMMessage(role="user", content=user_content),
    ]


def _build_revision_messages(
    *,
    plan: Plan,
    user_message: str,
    new_findings: Sequence[Finding],
    new_open_questions: Sequence[OpenQuestion] | None,
    system_prompt: str,
) -> List[LLMMessage]:
    schema_prompt = _schema_prompt()
    rubric_prompt = _load_prompt("rubrics.md")

    system_content = (
        f"{system_prompt}\nYou are revising an existing plan. {schema_prompt}\n{_JSON_ONLY_SUFFIX}"
    )

    payload = {
        "user_message": user_message,
        "existing_plan": plan.model_dump(),
        "new_findings": [finding.model_dump() for finding in new_findings],
        "new_open_questions": [
            question.model_dump() for question in new_open_questions or []
        ],
        "rubrics": rubric_prompt,
    }
    user_content = (
        "Update the plan given the new evidence. Maintain IDs when the underlying "
        "concept remains the same. Remove tasks and requirements that are satisfied, "
        "and add follow-up tasks if necessary.\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )

    return [
        LLMMessage(role="system", content=system_content),
        LLMMessage(role="user", content=user_content),
    ]


def _schema_prompt() -> str:
    return (
        "You MUST respond with a JSON object containing the keys: \n"
        "- problem_spec (string)\n"
        "- acceptance_criteria (array of strings)\n"
        "- requirements (array of objects with fields id, question, priority, quality_bar, stop_when_satisfied, metadata)\n"
        "- tasks (array of objects with fields id, requirement_id, description, tool, priority, budget_tokens, timeout_seconds, requires_approval, inputs, depends_on, metadata)\n"
        "- findings (array of objects with fields id, requirement_id (nullable), key, value, confidence, evidence_ids, metadata)\n"
        "- open_questions (array of objects with fields question, reason, requirement_id (nullable), suggested_tasks)\n"
        "- stop_conditions (array of strings)\n"
        "- metadata (object)"
    )


def _parse_plan_response(response: str, *, fallback_problem_spec: str) -> Dict[str, Any]:
    if not response:
        LOGGER.warning("Planner returned empty response")
        return {"problem_spec": fallback_problem_spec, "requirements": [], "tasks": []}
    try:
        parsed = _extract_json_object(response)
    except ValueError as exc:
        try:
            with open("/tmp/planner_failures.log", "a", encoding="utf-8") as handle:
                handle.write(response)
                handle.write("\n---\n")
        except OSError:  # pragma: no cover - best effort
            LOGGER.warning("Unable to write planner failure log")
        LOGGER.error(
            "Failed to parse planner response",
            exc_info=exc,
            extra={"planner_raw_response": response[:2000]},
        )
        LOGGER.error("Planner raw response snippet: %s", response[:2000])
        raise
    return parsed


_TRAILING_COMMA_RE = re.compile(r",(?=\s*[}\]])")


def _strip_trailing_commas(payload: str) -> str:
    previous = None
    current = payload
    while previous != current:
        previous = current
        current = _TRAILING_COMMA_RE.sub("", current)
    return current


def _extract_json_object(raw: str) -> Dict[str, Any]:
    candidates = [raw, _strip_trailing_commas(raw)]
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("Planner response does not contain valid JSON")
    cleaned = _strip_trailing_commas(match.group())
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise ValueError("Planner response JSON must be an object")
    return parsed


def _normalize_payload(payload: Dict[str, Any], problem_spec: str) -> Dict[str, Any]:
    normalized: Dict[str, Any] = {}
    normalized["problem_spec"] = str(payload.get("problem_spec") or problem_spec)
    normalized["acceptance_criteria"] = _ensure_str_list(
        payload.get("acceptance_criteria"),
        fallback=[
            "Final answer must cite evidence IDs",
            "Highlight any unresolved questions",
        ],
    )

    requirements = payload.get("requirements")
    normalized_requirements = _normalize_requirements(requirements, problem_spec)
    normalized["requirements"] = normalized_requirements

    requirement_ids = [req["id"] for req in normalized_requirements]

    tasks = payload.get("tasks")
    normalized["tasks"] = _normalize_tasks(tasks, requirement_ids, problem_spec)

    findings = payload.get("findings", [])
    normalized["findings"] = _normalize_findings(findings, requirement_ids)

    open_questions = payload.get("open_questions", [])
    normalized["open_questions"] = _normalize_open_questions(open_questions, requirement_ids)

    normalized["stop_conditions"] = _ensure_str_list(
        payload.get("stop_conditions"),
        fallback=["Acceptance criteria satisfied", "No new findings after follow-up"],
    )

    metadata = payload.get("metadata")
    normalized["metadata"] = metadata if isinstance(metadata, dict) else {}
    return normalized


def _ensure_str_list(value: Any, *, fallback: List[str]) -> List[str]:
    if isinstance(value, list):
        result = [str(item).strip() for item in value if str(item).strip()]
        return result or fallback
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return list(fallback)


def _normalize_requirements(value: Any, problem_spec: str) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        value = []
    normalized: List[Dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            continue
        requirement_id = str(item.get("id") or f"req-{index + 1}").strip()
        if not requirement_id:
            requirement_id = f"req-{index + 1}"
        question = str(item.get("question") or problem_spec).strip()
        priority = item.get("priority")
        try:
            priority_value = int(priority)
            if priority_value < 1:
                priority_value = 1
        except Exception:
            priority_value = 1
        quality_bar = str(
            item.get("quality_bar") or "At least one high-quality, citable source."
        ).strip()
        stop_when = bool(item.get("stop_when_satisfied", True))
        metadata = item.get("metadata")
        metadata_dict = metadata if isinstance(metadata, dict) else {}
        normalized.append(
            {
                "id": requirement_id,
                "question": question,
                "priority": priority_value,
                "quality_bar": quality_bar,
                "stop_when_satisfied": stop_when,
                "metadata": metadata_dict,
            }
        )
    if not normalized:
        normalized.append(
            {
                "id": "req-1",
                "question": problem_spec,
                "priority": 1,
                "quality_bar": "At least one high-quality, citable source.",
                "stop_when_satisfied": True,
                "metadata": {},
            }
        )
    return normalized


def _normalize_tasks(
    value: Any,
    requirement_ids: List[str],
    problem_spec: str,
) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        value = []
    catalog = get_agent_catalog()
    normalized: List[Dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            continue
        identifier = str(item.get("id") or f"task-{index + 1}").strip()
        if not identifier:
            identifier = f"task-{index + 1}"
        requirement_id = str(
            item.get("requirement_id") or requirement_ids[0]
        ).strip()
        if requirement_id not in requirement_ids:
            requirement_id = requirement_ids[0]
        description = str(
            item.get("description")
            or f"Investigate requirement {requirement_id}"
        ).strip()
        tool = str(item.get("tool") or "db_search").strip()
        if tool in {"db_keyword", "kb_search", "kb_lookup"}:
            tool = "db_search"
        priority = item.get("priority")
        try:
            priority_value = int(priority)
            if priority_value < 1:
                priority_value = 1
        except Exception:
            priority_value = 1
        descriptor = catalog.get(tool)
        default_budget = (
            descriptor.default_budget_tokens
            if descriptor and descriptor.default_budget_tokens
            else policies.DEFAULT_DB_BUDGET_TOKENS
        )
        default_timeout = (
            descriptor.default_timeout_seconds
            if descriptor and descriptor.default_timeout_seconds
            else policies.DEFAULT_DB_TIMEOUT_SECONDS
        )
        budget_tokens = item.get("budget_tokens", default_budget)
        timeout_seconds = item.get("timeout_seconds", default_timeout)
        try:
            budget_tokens = int(budget_tokens)
        except Exception:
            budget_tokens = default_budget
        if budget_tokens <= 0:
            budget_tokens = default_budget
        try:
            timeout_seconds = int(timeout_seconds)
        except Exception:
            timeout_seconds = default_timeout
        if timeout_seconds <= 0:
            timeout_seconds = default_timeout

        requires_approval = bool(
            item.get(
                "requires_approval",
                descriptor.requires_approval if descriptor else False,
            )
        )
        inputs = _normalize_task_inputs(item.get("inputs"), description, problem_spec)
        depends_on = _normalize_dependencies(item.get("depends_on"))
        metadata = item.get("metadata")
        metadata_dict = metadata if isinstance(metadata, dict) else {}

        normalized.append(
            {
                "id": identifier,
                "requirement_id": requirement_id,
                "description": description,
                "tool": tool,
                "priority": priority_value,
                "budget_tokens": budget_tokens,
                "timeout_seconds": timeout_seconds,
                "requires_approval": requires_approval,
                "inputs": inputs,
                "depends_on": depends_on,
                "metadata": metadata_dict,
            }
        )
    if not normalized:
        normalized.append(
            {
                "id": "task-1",
                "requirement_id": requirement_ids[0],
                "description": f"Search the knowledge base for: {problem_spec}",
                "tool": "db_search",
                "priority": 1,
                "budget_tokens": policies.DEFAULT_DB_BUDGET_TOKENS,
                "timeout_seconds": policies.DEFAULT_DB_TIMEOUT_SECONDS,
                "requires_approval": False,
                "inputs": {"query": problem_spec},
                "depends_on": [],
                "metadata": {},
            }
        )
    return normalized


def _normalize_findings(value: Any, requirement_ids: List[str]) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: List[Dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            continue
        finding_id = str(item.get("id") or f"finding-{index + 1}").strip()
        if not finding_id:
            finding_id = f"finding-{index + 1}"
        requirement_id = item.get("requirement_id")
        if requirement_id is not None:
            requirement_id = str(requirement_id).strip()
            if requirement_id and requirement_id not in requirement_ids:
                requirement_id = requirement_ids[0]
        key = str(item.get("key") or finding_id).strip()
        value_text = str(item.get("value") or "").strip()
        confidence = item.get("confidence", 0.5)
        try:
            confidence_value = float(confidence)
        except Exception:
            confidence_value = 0.5
        confidence_value = max(0.0, min(1.0, confidence_value))
        evidence_ids_raw = item.get("evidence_ids", [])
        evidence_ids = [
            str(eid).strip()
            for eid in evidence_ids_raw
            if isinstance(eid, (str, int)) and str(eid).strip()
        ]
        metadata = item.get("metadata")
        metadata_dict = metadata if isinstance(metadata, dict) else {}
        normalized.append(
            {
                "id": finding_id,
                "requirement_id": requirement_id,
                "key": key,
                "value": value_text,
                "confidence": confidence_value,
                "evidence_ids": evidence_ids,
                "metadata": metadata_dict,
            }
        )
    return normalized


def _normalize_open_questions(value: Any, requirement_ids: List[str]) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        if not question:
            continue
        reason = str(item.get("reason") or "").strip()
        requirement_id_raw = item.get("requirement_id")
        requirement_id: str | None
        if requirement_id_raw is None:
            requirement_id = None
        else:
            requirement_id = str(requirement_id_raw).strip() or None
            if requirement_id and requirement_id not in requirement_ids:
                requirement_id = requirement_ids[0]
        suggested = item.get("suggested_tasks", [])
        if isinstance(suggested, list):
            suggested_tasks = [
                str(task).strip()
                for task in suggested
                if isinstance(task, (str, int)) and str(task).strip()
            ]
        else:
            suggested_tasks = [str(suggested).strip()] if suggested else []
        normalized.append(
            {
                "question": question,
                "reason": reason,
                "requirement_id": requirement_id,
                "suggested_tasks": suggested_tasks,
            }
        )
    return normalized


def _normalize_task_inputs(
    value: Any,
    description: str,
    problem_spec: str,
) -> Dict[str, Any]:
    if not isinstance(value, dict):
        value = {}
    safe: Dict[str, Any] = {}
    for key, val in value.items():
        safe[str(key)] = val
    if "query" not in safe or not isinstance(safe["query"], str) or not safe["query"].strip():
        suggested = _suggest_query(description, context=problem_spec)
        if suggested:
            safe["query"] = suggested
    else:
        candidate = safe["query"].strip()
        if _needs_query_refinement(candidate):
            suggested = _suggest_query(description, context=problem_spec)
            if suggested:
                safe["query"] = suggested
    return safe


def _normalize_dependencies(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned or cleaned.lower().startswith("req"):
            return []
        return [cleaned]
    if isinstance(value, list):
        result = []
        for dep in value:
            cleaned = str(dep).strip()
            if cleaned and not cleaned.lower().startswith("req"):
                result.append(cleaned)
        return result
    return []


def _load_prompt(filename: str) -> str:
    cached = _PROMPT_CACHE.get(filename)
    if cached is not None:
        return cached
    path = _PROMPT_DIR / filename
    text = path.read_text(encoding="utf-8").strip()
    _PROMPT_CACHE[filename] = text
    return text


def _needs_query_refinement(query: str) -> bool:
    stripped = query.strip()
    if not stripped:
        return True
    if _QUERY_REWRITE_PREFIX_RE.match(stripped):
        return True
    if len(stripped.split()) > 14:
        return True
    return False


def _suggest_query(description: str, *, context: str | None = None) -> str:
    """Derive a default query string from a planner task description."""

    cleaned = description.strip()
    if not cleaned:
        return ""
    context_value = f"{cleaned} {context or ''}".strip()
    lowered = context_value.lower()
    subject, location = extract_subject_and_location(context_value)
    if subject:
        query_terms = [f'"{subject}"']
        if location:
            query_terms.append(location)
        if "sarasota" not in " ".join(query_terms).lower() and "sarasota" in lowered:
            query_terms.append("Sarasota Florida")
        if "florida" in lowered and "florida" not in " ".join(query_terms).lower():
            query_terms.append("Florida")
        if any(term in lowered for term in ("biograph", "background", "education")):
            query_terms.append("biography background")
        if any(term in lowered for term in ("news", "recent", "article")):
            query_terms.append("news")
        if any(term in lowered for term in ("ruling", "opinion", "case")):
            query_terms.append("notable cases")
        if "judge" not in subject.lower():
            query_terms.append("judge")
        return squeeze_whitespace(" ".join(query_terms))
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

_QUERY_REWRITE_PREFIX_RE = re.compile(
    r"^(?:search|find|perform|conduct|collect|gather|query|look(?:\s+up)?|retrieve)\b",
    re.IGNORECASE,
)


__all__ = ["plan_from_message", "revise_plan_with_evidence"]
