"""Configuration helpers for the chatbot service."""

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .constants import (
    ACCEPTANCE_CONFIDENCE_THRESHOLD,
    ACCEPTANCE_STRICT_MIN_SOURCES,
    MAX_WORKFLOW_ITERATIONS,
)

@dataclass(frozen=True)
class MCPServerConfig:
    """Configuration for a single MCP server endpoint."""

    name: str
    url: str
    tools: List[str]
    token_env: str | None = None
    token: str | None = None

    def with_token(self, token: str | None) -> "MCPServerConfig":
        return MCPServerConfig(
            name=self.name,
            url=self.url,
            tools=list(self.tools),
            token_env=self.token_env,
            token=token,
        )


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=4)
def _load_mcp_server_map(path: str) -> Dict[str, MCPServerConfig]:
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = _PROJECT_ROOT / config_path
    data = yaml.safe_load(config_path.read_text()) or {}
    servers: Dict[str, MCPServerConfig] = {}
    for name, entry in (data.get("servers") or {}).items():
        servers[name] = MCPServerConfig(
            name=name,
            url=entry["url"],
            tools=list(entry.get("tools", [])),
            token_env=entry.get("token_env"),
        )
    return servers


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
        extra="allow",
    )

    google_api_key: Optional[str] = Field(default=None, alias="GOOGLE_API_KEY")
    litellm_master_key: Optional[str] = Field(default=None, alias="LITELLM_MASTER_KEY")
    litellm_virtual_key: Optional[str] = Field(default=None, alias="LITELLM_VIRTUAL_KEY")

    database_url: str = Field(
        default="postgresql://user:pass@localhost:5432/agentdb",
        alias="DATABASE_URL",
    )

    postgres_mcp_token: Optional[str] = Field(default=None, alias="POSTGRES_MCP_TOKEN")
    neo4j_uri: str = Field(default="bolt://localhost:7687", alias="NEO4J_URI")
    neo4j_user: str = Field(default="neo4j", alias="NEO4J_USER")
    neo4j_password: Optional[str] = Field(default=None, alias="NEO4J_PASSWORD")
    neo4j_mcp_token: Optional[str] = Field(default=None, alias="NEO4J_MCP_TOKEN")

    litellm_base_url: str = Field(default="http://localhost:4000", alias="LITELLM_BASE_URL")
    litellm_timeout_seconds: float = Field(
        default=60.0, alias="LITELLM_TIMEOUT_SECONDS"
    )
    docling_base_url: str = Field(default="http://localhost:8000", alias="DOCLING_BASE_URL")
    docling_timeout_seconds: float = Field(
        default=120.0, alias="DOCLING_TIMEOUT_SECONDS"
    )
    docling_poll_interval_seconds: float = Field(
        default=5.0, alias="DOCLING_POLL_INTERVAL_SECONDS"
    )
    ingest_http_timeout_seconds: float = Field(
        default=15.0, alias="INGEST_HTTP_TIMEOUT_SECONDS"
    )
    searxng_internal_url: str = Field(
        default="http://searxng:8080", alias="SEARXNG_INTERNAL_URL"
    )
    openwebui_database_url: Optional[str] = Field(
        default=None, alias="OPENWEBUI_DATABASE_URL"
    )
    mcp_servers_path: str = Field(default="ops/mcp/servers.yaml", alias="MCP_SERVERS_PATH")
    agents_config_dir: str = Field(default="ops/agents", alias="AGENTS_CONFIG_DIR")
    prefect_api_url: Optional[str] = Field(default=None, alias="PREFECT_API_URL")
    prefect_server_ephemeral_enabled: bool = Field(
        default=True, alias="PREFECT_SERVER_EPHEMERAL_ENABLED"
    )
    prefect_server_ephemeral_startup_timeout_seconds: int = Field(
        default=60, alias="PREFECT_SERVER_EPHEMERAL_STARTUP_TIMEOUT_SECONDS"
    )
    tool_timeout_overrides: Dict[str, int] = Field(
        default_factory=dict, alias="TOOL_TIMEOUT_OVERRIDES"
    )
    af_max_iterations: int = Field(
        default=MAX_WORKFLOW_ITERATIONS, alias="AF_MAX_ITERATIONS"
    )
    acceptance_confidence_threshold: float = Field(
        default=ACCEPTANCE_CONFIDENCE_THRESHOLD,
        alias="ACCEPTANCE_CONFIDENCE_THRESHOLD",
    )
    acceptance_min_sources: int = Field(
        default=ACCEPTANCE_STRICT_MIN_SOURCES,
        alias="ACCEPTANCE_MIN_SOURCES",
    )

    def lite_llm_headers(self) -> Dict[str, str]:
        """Return default headers for LiteLLM proxy calls."""

        headers: Dict[str, str] = {}
        if self.litellm_virtual_key:
            headers["Authorization"] = f"Bearer {self.litellm_virtual_key}"
        return headers

    def model_aliases(self) -> Dict[str, str]:
        """Return a mapping of logical model roles to provider model identifiers."""
        from .runtime_config import get_runtime_config

        runtime_config = get_runtime_config()
        return {
            "planner": runtime_config.active("active_planner_model").identifier,
            "responder": runtime_config.active("active_responder_model").identifier,
            "cheap-worker": runtime_config.active("active_worker_model").identifier,
        }

    def resolve_path(self, relative_path: str) -> Path:
        """Resolve a repository-relative path to an absolute path."""

        candidate = Path(relative_path)
        if not candidate.is_absolute():
            candidate = _PROJECT_ROOT / candidate
        return candidate

    def mcp_server(self, name: str) -> MCPServerConfig:
        """Return the configuration for a named MCP server."""

        servers = _load_mcp_server_map(str(self.mcp_servers_path))
        if name not in servers:
            raise KeyError(f"Unknown MCP server: {name}")
        config = servers[name]
        token = None
        if config.token_env:
            attr_name = config.token_env.lower()
            token = getattr(self, attr_name, None)
            if not token:
                token = os.getenv(config.token_env)
        return config.with_token(token)

    def to_metadata(self) -> Dict[str, Any]:
        """Serialize non-sensitive settings for structured logging."""

        from .runtime_config import get_runtime_config

        return {
            "database_url": self.database_url,
            "neo4j_uri": self.neo4j_uri,
            "prefect_api_url": self.prefect_api_url,
            "prefect_server_ephemeral_enabled": self.prefect_server_ephemeral_enabled,
            "prefect_server_ephemeral_startup_timeout_seconds": self.prefect_server_ephemeral_startup_timeout_seconds,
            "af_max_iterations": self.af_max_iterations,
            "acceptance_confidence_threshold": self.acceptance_confidence_threshold,
            "acceptance_min_sources": self.acceptance_min_sources,
            "active_models": {
                key: model.identifier
                for key, model in get_runtime_config().active_models.items()
            },
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings."""

    return Settings()


__all__ = ["MCPServerConfig", "Settings", "get_settings"]
