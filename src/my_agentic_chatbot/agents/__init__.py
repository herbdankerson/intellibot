"""Agent configuration loader and catalog helpers."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError

from ..config import get_settings
from ..constants import (
    CUSTOM_TASK_PREFIX,
    DEFAULT_CUSTOM_AGENT_BUDGET_TOKENS,
    DEFAULT_CUSTOM_AGENT_TIMEOUT_SECONDS,
    DEFAULT_DB_BUDGET_TOKENS,
    DEFAULT_DB_TIMEOUT_SECONDS,
    DEFAULT_GRAPH_BUDGET_TOKENS,
    DEFAULT_GRAPH_MEMORY_BUDGET_TOKENS,
    DEFAULT_GRAPH_MEMORY_TIMEOUT_SECONDS,
    DEFAULT_GRAPH_MODELING_BUDGET_TOKENS,
    DEFAULT_GRAPH_MODELING_TIMEOUT_SECONDS,
    DEFAULT_GRAPH_TIMEOUT_SECONDS,
    DEFAULT_SEQUENTIAL_BUDGET_TOKENS,
    DEFAULT_SEQUENTIAL_TIMEOUT_SECONDS,
    DEFAULT_WEB_BUDGET_TOKENS,
    DEFAULT_WEB_TIMEOUT_SECONDS,
)
from ..storage.config_repo import get_registry

THINKING_BUDGET_MIN = 128
THINKING_BUDGET_MAX = 32768
MAX_OUTPUT_TOKENS = 65536
RESERVED_AGENT_NAMES = {"planner", "responder", "audit"}


_TOOL_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "db_search": {
        "description": "Hybrid ParadeDB search (BM25 + pgvector snippets)",
        "runtime": "mcp",
        "budget": DEFAULT_DB_BUDGET_TOKENS,
        "timeout": DEFAULT_DB_TIMEOUT_SECONDS,
        "planner_visible": True,
    },
    "web_search": {
        "description": "SearxNG toolbox search with fetch + summarize",
        "runtime": "native",
        "budget": DEFAULT_WEB_BUDGET_TOKENS,
        "timeout": DEFAULT_WEB_TIMEOUT_SECONDS,
        "planner_visible": False,
    },
    "graph_search": {
        "description": "Graph reasoning over Neo4j knowledge graph",
        "runtime": "mcp",
        "budget": DEFAULT_GRAPH_BUDGET_TOKENS,
        "timeout": DEFAULT_GRAPH_TIMEOUT_SECONDS,
        "requires_approval": True,
        "planner_visible": False,
    },
    "neo4j_cypher": {
        "description": "Neo4j Cypher query agent for graph exploration",
        "runtime": "mcp",
        "budget": DEFAULT_GRAPH_BUDGET_TOKENS,
        "timeout": DEFAULT_GRAPH_TIMEOUT_SECONDS,
        "requires_approval": True,
        "planner_visible": False,
    },
    "neo4j_memory": {
        "description": "Neo4j memory agent for entity & observation store",
        "runtime": "mcp",
        "budget": DEFAULT_GRAPH_MEMORY_BUDGET_TOKENS,
        "timeout": DEFAULT_GRAPH_MEMORY_TIMEOUT_SECONDS,
        "requires_approval": True,
        "planner_visible": False,
    },
    "neo4j_modeling": {
        "description": "Neo4j data-modeling agent (schema + validation)",
        "runtime": "mcp",
        "budget": DEFAULT_GRAPH_MODELING_BUDGET_TOKENS,
        "timeout": DEFAULT_GRAPH_MODELING_TIMEOUT_SECONDS,
        "requires_approval": True,
        "planner_visible": False,
    },
    "legal_search": {
        "description": "Legal knowledge MCP search (ParadeDB legal corpus)",
        "runtime": "mcp",
        "budget": DEFAULT_DB_BUDGET_TOKENS,
        "timeout": DEFAULT_DB_TIMEOUT_SECONDS,
        "planner_visible": False,
    },
    "agent-sequentialthinking": {
        "description": "Sequential Thinking MCP agent for reflective planning",
        "runtime": "mcp",
        "budget": DEFAULT_SEQUENTIAL_BUDGET_TOKENS,
        "timeout": DEFAULT_SEQUENTIAL_TIMEOUT_SECONDS,
        "planner_visible": True,
    },
}


class AgentGenerationConfig(BaseModel):
    """Gemini generation configuration for an agent."""

    response_mime_type: Literal["text/plain", "application/json"] = Field(
        alias="response_mime_type"
    )
    max_output_tokens: int = Field(alias="max_output_tokens", ge=1, le=MAX_OUTPUT_TOKENS)
    thinking_mode: Literal["dynamic", "disabled", "fixed"] = Field(alias="thinking_mode")
    thinking_budget_tokens: int = Field(
        alias="thinking_budget_tokens",
        ge=THINKING_BUDGET_MIN,
        le=THINKING_BUDGET_MAX,
        default=THINKING_BUDGET_MAX,
    )

    def build_generation_payload(
        self,
        *,
        include_thoughts: bool,
        max_output_override: Optional[int] = None,
        thinking_budget_override: Optional[int] = None,
    ) -> Dict[str, object]:
        """Return a LiteLLM-compatible generationConfig payload."""

        max_tokens = max_output_override or self.max_output_tokens
        max_tokens = max(1, min(max_tokens, self.max_output_tokens))
        payload: Dict[str, object] = {
            "responseMimeType": self.response_mime_type,
            "maxOutputTokens": max_tokens,
        }

        if include_thoughts:
            payload.setdefault("responseModalities", ["TEXT"])

        budget = thinking_budget_override or self.thinking_budget_tokens
        budget = max(THINKING_BUDGET_MIN, min(budget, THINKING_BUDGET_MAX))

        mode = self.thinking_mode
        if mode == "dynamic":
            payload["thinkingConfig"] = {"thinkingBudget": -1}
        elif mode == "disabled":
            payload["thinkingConfig"] = {"thinkingBudget": 0}
        else:
            payload["thinkingConfig"] = {"thinkingBudget": budget}

        return payload


class AgentConfig(BaseModel):
    """Agent configuration wrapper."""

    name: str
    model: str
    include_thoughts: bool = False
    generation: AgentGenerationConfig
    system_prompt: Optional[str] = None
    type: Literal["base", "planner", "custom"] = "base"
    params: Dict[str, Any] = Field(default_factory=dict)
    tool_overrides: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    db_scope: List[str] = Field(default_factory=list)
    enabled: bool = True
    notes: Optional[str] = None

    def build_generation_payload(
        self,
        *,
        max_output_override: Optional[int] = None,
        thinking_budget_override: Optional[int] = None,
        include_thoughts_override: Optional[bool] = None,
    ) -> Dict[str, object]:
        include_thoughts = (
            self.include_thoughts if include_thoughts_override is None else include_thoughts_override
        )
        return self.generation.build_generation_payload(
            include_thoughts=include_thoughts,
            max_output_override=max_output_override,
            thinking_budget_override=thinking_budget_override,
        )


@dataclass(frozen=True)
class AgentDescriptor:
    """Metadata about an executable agent/tool that planners may target."""

    tool: str
    description: str
    runtime: Literal["mcp", "llm", "native"]
    default_budget_tokens: int
    default_timeout_seconds: int
    requires_approval: bool = False
    planner_visible: bool = True
    agent_config_name: str | None = None

    def planner_hint(self) -> str:
        """Return a formatted hint describing this agent for planner prompts."""

        approval = " (requires approval)" if self.requires_approval else ""
        return (
            f"{self.tool}: {self.description} — budget {self.default_budget_tokens} tokens, "
            f"timeout {self.default_timeout_seconds}s{approval}"
        )


def _merge_dicts(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        if (
            isinstance(value, dict)
            and isinstance(result.get(key), dict)
        ):
            result[key] = _merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


@lru_cache(maxsize=32)
def _load_agent_overlay(path: Path) -> Dict[str, Any]:
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):  # pragma: no cover - configuration issue
        raise ValueError(f"Agent overlay at {path} must be a mapping")
    return raw


def _overlay_directory() -> Path:
    settings = get_settings()
    return settings.resolve_path(settings.agents_config_dir)


def _load_overlay_for(agent_name: str) -> Dict[str, Any]:
    directory = _overlay_directory()
    path = directory / f"{agent_name}.yaml"
    if not path.exists():
        return {}
    return _load_agent_overlay(path)


def _overlay_agent_names() -> Iterable[str]:
    directory = _overlay_directory()
    if not directory.exists():
        return []
    names: List[str] = []
    for path in directory.glob("*.yaml"):
        if path.name == "schema.yaml":
            continue
        data = _load_agent_overlay(path)
        name = data.get("name") or path.stem
        names.append(str(name))
    return names


def _payload_from_record(record) -> Dict[str, Any]:
    params = dict(record.params)
    model_alias = params.get("model") or record.model_alias or record.name
    generation = params.get("generation") or {}
    include_thoughts = params.get("include_thoughts")
    payload: Dict[str, Any] = {
        "name": record.name,
        "model": model_alias,
        "include_thoughts": bool(include_thoughts) if include_thoughts is not None else False,
        "generation": generation,
        "system_prompt": record.system_prompt,
        "type": record.type,
        "params": params,
        "tool_overrides": record.tool_bindings,
        "db_scope": record.db_scope,
        "enabled": record.enabled,
        "notes": record.notes,
    }
    if not payload["generation"]:
        raise ValueError(
            f"Agent '{record.name}' missing generation configuration in cfg.agents.params"
        )
    return payload


@lru_cache(maxsize=8)
def get_agent_config(agent_name: str) -> AgentConfig:
    """Load agent configuration from the database with optional YAML overlay."""

    registry = get_registry()
    base_payload: Dict[str, Any]
    try:
        record = registry.require_agent(agent_name)
    except RuntimeError:
        overlay = _load_overlay_for(agent_name)
        if not overlay:
            raise
        base_payload = overlay
    else:
        base_payload = _payload_from_record(record)
        overlay = _load_overlay_for(agent_name)
        if overlay:
            base_payload = _merge_dicts(base_payload, overlay)

    try:
        return AgentConfig.model_validate(base_payload)
    except ValidationError as exc:  # pragma: no cover - configuration issue
        raise ValueError(f"Invalid agent configuration for '{agent_name}': {exc}") from exc


@lru_cache(maxsize=4)
def list_agent_configs() -> Dict[str, AgentConfig]:
    """Return all known agent configurations keyed by logical name."""

    registry = get_registry()
    configs: Dict[str, AgentConfig] = {}
    for name in registry.agents.keys():
        configs[name] = get_agent_config(name)
    for name in _overlay_agent_names():
        configs.setdefault(name, get_agent_config(name))
    return configs


@lru_cache(maxsize=1)
def get_agent_catalog() -> Dict[str, AgentDescriptor]:
    """Build a map of tool identifiers to planner-visible agent descriptors."""

    registry = get_registry()
    catalog: Dict[str, AgentDescriptor] = {}
    for slug, template in _TOOL_TEMPLATES.items():
        tool_record = registry.tools.get(slug)
        if tool_record is None or not tool_record.enabled:
            continue
        catalog[slug] = AgentDescriptor(
            tool=slug,
            description=template["description"],
            runtime=template["runtime"],
            default_budget_tokens=template["budget"],
            default_timeout_seconds=template["timeout"],
            requires_approval=template.get("requires_approval", False),
            planner_visible=template.get("planner_visible", True),
        )

    for name, config in list_agent_configs().items():
        if name in RESERVED_AGENT_NAMES or not config.enabled:
            continue
        if config.type != "custom":
            continue
        tool_name = f"{CUSTOM_TASK_PREFIX}-{config.name}" if CUSTOM_TASK_PREFIX else config.name
        catalog[tool_name] = AgentDescriptor(
            tool=tool_name,
            description=f"LLM agent '{config.name}' using model {config.model}",
            runtime="llm",
            default_budget_tokens=DEFAULT_CUSTOM_AGENT_BUDGET_TOKENS,
            default_timeout_seconds=DEFAULT_CUSTOM_AGENT_TIMEOUT_SECONDS,
            agent_config_name=config.name,
        )

    return catalog


def planner_tool_hints() -> List[str]:
    """Return formatted strings describing available tools for prompt inclusion."""

    return [
        descriptor.planner_hint()
        for descriptor in get_agent_catalog().values()
        if descriptor.planner_visible
    ]


def iter_custom_agent_descriptors() -> Iterable[AgentDescriptor]:
    """Yield descriptors for LLM-based custom agents only."""

    for descriptor in get_agent_catalog().values():
        if descriptor.runtime == "llm" and descriptor.agent_config_name:
            yield descriptor


__all__ = [
    "AgentConfig",
    "AgentDescriptor",
    "AgentGenerationConfig",
    "get_agent_catalog",
    "get_agent_config",
    "iter_custom_agent_descriptors",
    "list_agent_configs",
    "planner_tool_hints",
]
