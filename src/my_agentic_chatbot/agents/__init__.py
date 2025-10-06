"""Agent configuration loader and catalog helpers."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Literal, Optional

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

THINKING_BUDGET_MIN = 128
THINKING_BUDGET_MAX = 32768
MAX_OUTPUT_TOKENS = 65536
RESERVED_AGENT_NAMES = {"planner", "responder", "audit"}


class AgentGenerationConfig(BaseModel):
    """Gemini generation configuration for an agent."""

    response_mime_type: Literal["text/plain"] = Field(alias="response_mime_type")
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
    runtime: Literal["mcp", "llm"]
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


@lru_cache(maxsize=32)
def _load_agent_config(path: Path) -> AgentConfig:
    raw = yaml.safe_load(path.read_text()) or {}
    try:
        return AgentConfig.model_validate(raw)
    except ValidationError as exc:  # pragma: no cover - configuration issue
        raise ValueError(f"Invalid agent configuration at {path}: {exc}") from exc


@lru_cache(maxsize=8)
def get_agent_config(agent_name: str) -> AgentConfig:
    """Load agent configuration from disk."""

    settings = get_settings()
    agent_dir = settings.resolve_path(settings.agents_config_dir)
    path = agent_dir / f"{agent_name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Agent configuration not found for '{agent_name}' at {path}")
    return _load_agent_config(path)


@lru_cache(maxsize=4)
def list_agent_configs() -> Dict[str, AgentConfig]:
    """Return all agent configurations keyed by their logical name."""

    settings = get_settings()
    agent_dir = settings.resolve_path(settings.agents_config_dir)
    if not agent_dir.exists():
        return {}

    configs: Dict[str, AgentConfig] = {}
    for path in agent_dir.glob("*.yaml"):
        if path.name == "schema.yaml":
            continue
        config = _load_agent_config(path)
        configs[config.name] = config
    return configs


@lru_cache(maxsize=1)
def get_agent_catalog() -> Dict[str, AgentDescriptor]:
    """Build a map of tool identifiers to planner-visible agent descriptors."""

    catalog: Dict[str, AgentDescriptor] = {
        "db_search": AgentDescriptor(
            tool="db_search",
            description="Hybrid ParadeDB search (BM25 + pgvector snippets)",
            runtime="mcp",
            default_budget_tokens=DEFAULT_DB_BUDGET_TOKENS,
            default_timeout_seconds=DEFAULT_DB_TIMEOUT_SECONDS,
        ),
        "web_search": AgentDescriptor(
            tool="web_search",
            description="SearxNG toolbox search with fetch + summarize",
            runtime="mcp",
            default_budget_tokens=DEFAULT_WEB_BUDGET_TOKENS,
            default_timeout_seconds=DEFAULT_WEB_TIMEOUT_SECONDS,
            planner_visible=False,
        ),
        "neo4j_cypher": AgentDescriptor(
            tool="neo4j_cypher",
            description="Neo4j Cypher query agent for graph exploration",
            runtime="mcp",
            default_budget_tokens=DEFAULT_GRAPH_BUDGET_TOKENS,
            default_timeout_seconds=DEFAULT_GRAPH_TIMEOUT_SECONDS,
            requires_approval=True,
            planner_visible=False,
        ),
        "neo4j_memory": AgentDescriptor(
            tool="neo4j_memory",
            description="Neo4j memory agent for entity & observation store",
            runtime="mcp",
            default_budget_tokens=DEFAULT_GRAPH_MEMORY_BUDGET_TOKENS,
            default_timeout_seconds=DEFAULT_GRAPH_MEMORY_TIMEOUT_SECONDS,
            requires_approval=True,
            planner_visible=False,
        ),
        "neo4j_modeling": AgentDescriptor(
            tool="neo4j_modeling",
            description="Neo4j data-modeling agent (schema + validation)",
            runtime="mcp",
            default_budget_tokens=DEFAULT_GRAPH_MODELING_BUDGET_TOKENS,
            default_timeout_seconds=DEFAULT_GRAPH_MODELING_TIMEOUT_SECONDS,
            requires_approval=True,
            planner_visible=False,
        ),
        "legal_search": AgentDescriptor(
            tool="legal_search",
            description="Legal knowledge MCP search (ParadeDB legal corpus)",
            runtime="mcp",
            default_budget_tokens=DEFAULT_DB_BUDGET_TOKENS,
            default_timeout_seconds=DEFAULT_DB_TIMEOUT_SECONDS,
            planner_visible=False,
        ),
        "agent-sequentialthinking": AgentDescriptor(
            tool="agent-sequentialthinking",
            description="Sequential Thinking MCP agent for reflective planning",
            runtime="mcp",
            default_budget_tokens=DEFAULT_SEQUENTIAL_BUDGET_TOKENS,
            default_timeout_seconds=DEFAULT_SEQUENTIAL_TIMEOUT_SECONDS,
            planner_visible=True,
        ),
    }

    for name, config in list_agent_configs().items():
        if name in RESERVED_AGENT_NAMES:
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
