"""Agent configuration loader for planner/responder/workers."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Dict, Literal, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError

from ..config import get_settings


THINKING_BUDGET_MIN = 128
THINKING_BUDGET_MAX = 32768
MAX_OUTPUT_TOKENS = 65536


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


__all__ = ["AgentConfig", "AgentGenerationConfig", "get_agent_config"]
