"""Database-backed runtime configuration loader for the agent framework."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, Iterable, Mapping, Optional

from sqlalchemy import text

from .storage.connection import get_engine

LOGGER = logging.getLogger(__name__)

_TEMPLATE_PATTERN = re.compile(r"\$\{(?P<name>[A-Z0-9_]+)\}")

_REQUIRED_ACTIVE_KEYS = {
    "active_planner_model",
    "active_responder_model",
    "active_worker_model",
    "active_emb_general",
    "active_emb_legal",
    "active_emb_code",
}


@dataclass(frozen=True)
class ModelConfig:
    """Configuration for a single model entry in ``cfg.models``."""

    name: str
    provider: str
    identifier: str
    uri_template: Optional[str]
    resolved_uri: Optional[str]
    dims: Optional[int]
    purpose: str
    enabled: bool
    version: Optional[str]
    notes: Optional[str]
    config: Dict[str, Any]

    def require_dims(self) -> int:
        """Return the embedding dimensionality, raising if undefined."""

        if self.dims is None:
            raise RuntimeError(f"Model '{self.name}' missing dimensionality metadata")
        return self.dims


@dataclass(frozen=True)
class ToolConfig:
    """Configuration for a single tool entry in ``cfg.tools``."""

    name: str
    type: str
    endpoint_template: str
    resolved_endpoint: str
    method: str
    auth_ref: Optional[str]
    timeout_s: Optional[int]
    config: Dict[str, Any]
    enabled: bool


@dataclass(frozen=True)
class RuntimeConfig:
    """Aggregated runtime configuration resolved from the database."""

    models: Dict[str, ModelConfig]
    tools: Dict[str, ToolConfig]
    active_models: Dict[str, ModelConfig]

    def model(self, name: str) -> ModelConfig:
        try:
            return self.models[name]
        except KeyError as exc:  # pragma: no cover - defensive
            raise RuntimeError(f"Model '{name}' not registered in cfg.models") from exc

    def active(self, key: str) -> ModelConfig:
        try:
            return self.active_models[key]
        except KeyError as exc:
            raise RuntimeError(f"Active model key '{key}' is not configured") from exc


def _resolve_template(template: Optional[str], env: Mapping[str, str]) -> Optional[str]:
    if template is None:
        return None

    def replacer(match: re.Match[str]) -> str:
        name = match.group("name")
        if name not in env:
            raise RuntimeError(
                f"Environment variable '{name}' required to resolve template '{template}'"
            )
        return env[name]

    return _TEMPLATE_PATTERN.sub(replacer, template)


def _fetch_active_map(connection) -> Dict[str, str]:
    rows = connection.execute(text("SELECT key, value FROM cfg.active")).mappings()
    active: Dict[str, str] = {}
    for row in rows:
        active[row["key"].strip()] = row["value"].strip()
    missing = _REQUIRED_ACTIVE_KEYS.difference(active.keys())
    if missing:
        raise RuntimeError(
            "Missing required cfg.active keys: " + ", ".join(sorted(missing))
        )
    return active


def _fetch_models(connection, env: Mapping[str, str]) -> Dict[str, ModelConfig]:
    rows = connection.execute(
        text(
            """
            SELECT m.alias, m.name AS identifier, m.endpoint, m.purpose, m.dims,
                   m.enabled, COALESCE(m.default_params, '{}'::jsonb) AS params,
                   p.slug AS provider_slug
            FROM cfg.models AS m
            JOIN cfg.providers AS p ON p.id = m.provider_id
            """
        )
    ).mappings()
    models: Dict[str, ModelConfig] = {}
    for row in rows:
        alias = (row.get("alias") or row["identifier"]).strip()
        models[alias] = ModelConfig(
            name=alias,
            provider=row["provider_slug"].strip(),
            identifier=row["identifier"].strip(),
            uri_template=row.get("endpoint"),
            resolved_uri=_resolve_template(row.get("endpoint"), env),
            dims=row.get("dims"),
            purpose=(row.get("purpose") or "chat").strip(),
            enabled=bool(row.get("enabled")),
            version=None,
            notes=None,
            config=dict(row.get("params") or {}),
        )
    if not models:
        raise RuntimeError("cfg.models is empty; runtime configuration cannot continue")
    return models


def _fetch_tools(connection, env: Mapping[str, str]) -> Dict[str, ToolConfig]:
    rows = connection.execute(
        text(
            """
            SELECT slug, kind, manifest_or_ref, default_params, enabled
            FROM cfg.tools
            """
        )
    ).mappings()
    tools: Dict[str, ToolConfig] = {}
    for row in rows:
        name = row["slug"].strip()
        template = row["manifest_or_ref"]
        resolved = _resolve_template(template, env)
        params = dict(row.get("default_params") or {})
        tools[name] = ToolConfig(
            name=name,
            type=row["kind"].strip(),
            endpoint_template=template,
            resolved_endpoint=resolved,
            method=str(params.get("method", "POST")),
            auth_ref=params.get("auth_ref"),
            timeout_s=params.get("timeout"),
            config=params,
            enabled=bool(row["enabled"]),
        )
    return tools


def _build_active_models(
    active_map: Mapping[str, str], models: Mapping[str, ModelConfig]
) -> Dict[str, ModelConfig]:
    output: Dict[str, ModelConfig] = {}
    for key, model_name in active_map.items():
        try:
            model = models[model_name]
        except KeyError as exc:
            raise RuntimeError(
                f"cfg.active key '{key}' references unknown model '{model_name}'"
            ) from exc
        if not model.enabled:
            raise RuntimeError(
                f"cfg.active key '{key}' points to disabled model '{model_name}'"
            )
        output[key] = model
    return output


def load_runtime_config(env: Mapping[str, str] | None = None) -> RuntimeConfig:
    """Load runtime configuration from Postgres, resolving environment templates."""

    env_map = env or os.environ
    engine = get_engine()
    try:
        with engine.connect() as connection:
            active_map = _fetch_active_map(connection)
            models = _fetch_models(connection, env_map)
            tools = _fetch_tools(connection, env_map)
    except Exception as exc:  # pragma: no cover - defensive logging
        LOGGER.error(
            "Failed to load runtime configuration from database",
            exc_info=exc,
        )
        raise RuntimeError(
            "Unable to load runtime configuration. Ensure Postgres is reachable and"
            " cfg.* tables are seeded."
        ) from exc
    active_models = _build_active_models(active_map, models)
    LOGGER.info(
        "Runtime configuration loaded",
        extra={
            "active_models": {key: model.name for key, model in active_models.items()},
            "emb_dims": {
                key: model.dims
                for key, model in active_models.items()
                if model.purpose == "embedding"
            },
        },
    )
    return RuntimeConfig(models=models, tools=tools, active_models=active_models)


@lru_cache(maxsize=1)
def get_runtime_config() -> RuntimeConfig:
    """Return cached runtime configuration."""

    return load_runtime_config()


def refresh_runtime_config() -> None:
    """Invalidate the cached runtime configuration."""

    get_runtime_config.cache_clear()  # type: ignore[attr-defined]


__all__ = [
    "ModelConfig",
    "RuntimeConfig",
    "ToolConfig",
    "get_runtime_config",
    "load_runtime_config",
    "refresh_runtime_config",
    "get_active_model_identifier",
    "get_active_model_dims",
    "get_tool_config",
]


def get_active_model_identifier(key: str) -> str:
    """Return the provider identifier for the active model bound to ``key``."""

    return get_runtime_config().active(key).identifier


def get_active_model_dims(key: str) -> int:
    """Return the embedding dimensionality for the active model bound to ``key``."""

    return get_runtime_config().active(key).require_dims()


def get_tool_config(name: str) -> ToolConfig:
    """Return the configuration for a registered tool, ensuring it is enabled."""

    runtime = get_runtime_config()
    try:
        tool = runtime.tools[name]
    except KeyError as exc:  # pragma: no cover - safety
        raise RuntimeError(f"Tool '{name}' is not registered in cfg.tools") from exc
    if not tool.enabled:
        raise RuntimeError(f"Tool '{name}' is disabled in cfg.tools")
    return tool


# The runtime configuration must be sourced from the live database. Static
# fallbacks previously used for offline tests were intentionally removed to make
# configuration drift immediately visible during startup.
