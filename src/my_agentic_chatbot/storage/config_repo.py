"""Database-backed configuration registry for providers, models, agents, and tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import logging
import os
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

from sqlalchemy import text

from .db import get_engine

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderRecord:
    """Row from ``cfg.providers``."""

    id: int
    slug: str
    notes: Optional[str]


@dataclass(frozen=True)
class ModelRecord:
    """Row from ``cfg.models`` joined with its provider."""

    id: int
    alias: str
    name: str
    provider_slug: str
    endpoint: Optional[str]
    purpose: str
    dims: Optional[int]
    default_params: Dict[str, Any]
    pricing: Dict[str, Any]
    enabled: bool


@dataclass(frozen=True)
class ToolRecord:
    """Row from ``cfg.tools``."""

    id: int
    slug: str
    kind: str
    manifest_or_ref: str
    default_params: Dict[str, Any]
    enabled: bool
    notes: Optional[str]

    @property
    def is_mcp(self) -> bool:
        return self.kind == "mcp"


@dataclass
class AgentRecord:
    """Row from ``cfg.agents`` with inlined tool bindings."""

    id: int
    name: str
    type: str
    model_alias: Optional[str]
    system_prompt: Optional[str]
    params: Dict[str, Any]
    tools_profile: Optional[str]
    enabled: bool
    notes: Optional[str]
    db_scope: List[str] = field(default_factory=list)
    tool_bindings: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def tool_overrides(self, tool_slug: str) -> Dict[str, Any]:
        return self.tool_bindings.get(tool_slug, {})


@dataclass(frozen=True)
class RegistrySnapshot:
    """In-memory snapshot of all configuration tables."""

    providers: Dict[str, ProviderRecord]
    models: Dict[str, ModelRecord]
    tools: Dict[str, ToolRecord]
    agents: Dict[str, AgentRecord]

    def require_model(self, alias: str) -> ModelRecord:
        try:
            return self.models[alias]
        except KeyError as exc:  # pragma: no cover - defensive
            raise RuntimeError(f"Model alias '{alias}' is not registered") from exc

    def require_agent(self, name: str) -> AgentRecord:
        try:
            return self.agents[name]
        except KeyError as exc:  # pragma: no cover - defensive
            raise RuntimeError(f"Agent '{name}' is not registered") from exc

    def agent_tools(self, name: str) -> Dict[str, Dict[str, Any]]:
        return dict(self.require_agent(name).tool_bindings)



_TEMPLATE_PATTERN = re.compile(r"\$\{(?P<name>[A-Z0-9_]+)\}")


def _resolve_template(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None

    def repl(match: re.Match[str]) -> str:
        env_name = match.group("name")
        try:
            return os.environ[env_name]
        except KeyError as exc:
            raise RuntimeError(
                f"Environment variable '{env_name}' is required to resolve '{value}'"
            ) from exc

    return _TEMPLATE_PATTERN.sub(repl, value)


def _load_providers(connection) -> Dict[int, ProviderRecord]:
    rows = connection.execute(
        text(
            "SELECT id, slug, notes FROM cfg.providers"
        )
    ).mappings()
    providers: Dict[int, ProviderRecord] = {}
    for row in rows:
        record = ProviderRecord(
            id=row["id"],
            slug=row["slug"].strip(),
            notes=row.get("notes"),
        )
        providers[record.id] = record
    return providers


def _normalize_alias(alias: Optional[str], fallback: str) -> str:
    alias = (alias or fallback).strip()
    return alias


def _load_models(connection, providers: Mapping[int, ProviderRecord]) -> Dict[str, ModelRecord]:
    rows = connection.execute(
        text(
            """
            SELECT m.id, m.alias, m.name, m.endpoint, m.purpose, m.dims,
                   m.default_params, m.pricing, m.enabled,
                   p.slug AS provider_slug
            FROM cfg.models AS m
            JOIN cfg.providers AS p ON p.id = m.provider_id
            """
        )
    ).mappings()
    models: Dict[str, ModelRecord] = {}
    for row in rows:
        alias = _normalize_alias(row.get("alias"), row["name"])
        models[alias] = ModelRecord(
            id=row["id"],
            alias=alias,
            name=row["name"].strip(),
            provider_slug=row["provider_slug"].strip(),
            endpoint=_resolve_template(row.get("endpoint")),
            purpose=(row.get("purpose") or "chat").strip(),
            dims=row.get("dims"),
            default_params=dict(row.get("default_params") or {}),
            pricing=dict(row.get("pricing") or {}),
            enabled=bool(row.get("enabled")),
        )
    return models


def _load_tools(connection) -> Dict[str, ToolRecord]:
    rows = connection.execute(
        text(
            """
            SELECT id, slug, kind, manifest_or_ref, default_params, enabled, notes
            FROM cfg.tools
            """
        )
    ).mappings()
    tools: Dict[str, ToolRecord] = {}
    for row in rows:
        slug = row["slug"].strip()
        tools[slug] = ToolRecord(
            id=row["id"],
            slug=slug,
            kind=row["kind"].strip(),
            manifest_or_ref=_resolve_template(row.get("manifest_or_ref")),
            default_params=dict(row.get("default_params") or {}),
            enabled=bool(row.get("enabled")),
            notes=row.get("notes"),
        )
    return tools


def _load_agents(connection) -> Dict[int, AgentRecord]:
    rows = connection.execute(
        text(
            """
            SELECT id,
                   name,
                   type,
                   model_alias,
                   system_prompt,
                   params,
                   tools_profile,
                   enabled,
                   notes,
                   COALESCE(db_scope, '[]'::jsonb) AS db_scope
            FROM cfg.agents
            """
        )
    ).mappings()
    agents: Dict[int, AgentRecord] = {}
    for row in rows:
        identifier = row["id"]
        raw_scope = row.get("db_scope") or []
        scope: List[str] = []
        if isinstance(raw_scope, list):
            scope = [str(value).strip() for value in raw_scope if str(value).strip()]
        elif isinstance(raw_scope, str):
            scope = [segment.strip() for segment in raw_scope.split(",") if segment.strip()]
        agents[identifier] = AgentRecord(
            id=identifier,
            name=row["name"].strip(),
            type=row["type"].strip(),
            model_alias=(row.get("model_alias") or None),
            system_prompt=row.get("system_prompt"),
            params=dict(row.get("params") or {}),
            tools_profile=row.get("tools_profile"),
            enabled=bool(row.get("enabled")),
            notes=row.get("notes"),
            db_scope=scope,
        )
    return agents


def _attach_agent_tools(connection, agents: Mapping[int, AgentRecord], tools: Mapping[str, ToolRecord]) -> None:
    if not agents:
        return
    rows = connection.execute(
        text(
            """
            SELECT at.agent_id, t.slug, COALESCE(at.overrides, '{}'::jsonb) AS overrides
            FROM cfg.agent_tools AS at
            JOIN cfg.tools AS t ON t.id = at.tool_id
            """
        )
    ).mappings()
    for row in rows:
        agent_id = row["agent_id"]
        agent = agents.get(agent_id)
        if agent is None:
            continue
        slug = row["slug"].strip()
        overrides = dict(row.get("overrides") or {})
        agent.tool_bindings[slug] = overrides


def load_registry() -> RegistrySnapshot:
    """Load all configuration tables from the database."""

    engine = get_engine()
    try:
        with engine.connect() as connection:
            providers = _load_providers(connection)
            models = _load_models(connection, providers)
            tools = _load_tools(connection)
            agents_by_id = _load_agents(connection)
            _attach_agent_tools(connection, agents_by_id, tools)
    except Exception as exc:  # pragma: no cover - defensive logging
        LOGGER.error(
            "Failed to load configuration registry from database",
            exc_info=exc,
        )
        raise RuntimeError(
            "Unable to load registry configuration. Ensure Postgres is reachable and cfg.* tables are seeded."
        ) from exc

    agents = {record.name: record for record in agents_by_id.values()}
    providers_by_slug = {record.slug: record for record in providers.values()}
    return RegistrySnapshot(
        providers=providers_by_slug,
        models=models,
        tools=tools,
        agents=agents,
    )


@lru_cache(maxsize=1)
def get_registry() -> RegistrySnapshot:
    """Return a cached registry snapshot."""

    return load_registry()


def refresh_registry() -> None:
    """Invalidate the cached registry snapshot."""

    get_registry.cache_clear()  # type: ignore[attr-defined]


__all__ = [
    "AgentRecord",
    "ModelRecord",
    "ProviderRecord",
    "RegistrySnapshot",
    "ToolRecord",
    "get_registry",
    "load_registry",
    "refresh_registry",
]
