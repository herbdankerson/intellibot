"""Configuration helpers for the chatbot service."""

from functools import lru_cache
from typing import Any, Dict, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", populate_by_name=True
    )

    openai_key_primary: Optional[str] = Field(default=None, alias="OPENAI_KEY_1")
    openai_key_secondary: Optional[str] = Field(default=None, alias="OPENAI_KEY_2")
    anthropic_key: Optional[str] = Field(default=None, alias="ANTHROPIC_KEY")
    google_api_key: Optional[str] = Field(default=None, alias="GOOGLE_API_KEY")
    cohere_key: Optional[str] = Field(default=None, alias="COHERE_KEY")

    database_url: str = Field(
        default="postgresql://user:pass@localhost:5432/agentdb",
        alias="DATABASE_URL",
    )

    postgres_mcp_token: Optional[str] = Field(default=None, alias="POSTGRES_MCP_TOKEN")
    neo4j_uri: str = Field(default="bolt://localhost:7687", alias="NEO4J_URI")
    neo4j_user: str = Field(default="neo4j", alias="NEO4J_USER")
    neo4j_password: Optional[str] = Field(default=None, alias="NEO4J_PASSWORD")
    neo4j_mcp_token: Optional[str] = Field(default=None, alias="NEO4J_MCP_TOKEN")

    planner_model: str = Field(default="planner")
    responder_model: str = Field(default="responder")
    cheap_worker_model: str = Field(default="cheap-worker")

    def lite_llm_headers(self) -> Dict[str, str]:
        """Return default headers for LiteLLM proxy calls."""

        headers: Dict[str, str] = {}
        if self.openai_key_primary:
            headers["Authorization"] = f"Bearer {self.openai_key_primary}"
        return headers

    def model_aliases(self) -> Dict[str, str]:
        """Return a mapping of logical model roles to provider model identifiers."""

        return {
            "planner": self.planner_model,
            "responder": self.responder_model,
            "cheap-worker": self.cheap_worker_model,
        }

    def to_metadata(self) -> Dict[str, Any]:
        """Serialize non-sensitive settings for structured logging."""

        return {
            "database_url": self.database_url,
            "neo4j_uri": self.neo4j_uri,
            "planner_model": self.planner_model,
            "responder_model": self.responder_model,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings."""

    return Settings()


__all__ = ["Settings", "get_settings"]
