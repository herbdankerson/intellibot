"""LiteLLM proxy client used for planner and responder calls."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

import httpx

from ..agents import AgentConfig
from ..config import get_settings

LOGGER = logging.getLogger(__name__)


@dataclass
class LLMMessage:
    """Container for OpenAI-compatible chat messages."""

    role: str
    content: str

    def as_dict(self) -> Dict[str, str]:
        return {"role": self.role, "content": self.content}


class LLMClient:
    """Small synchronous client that talks to a LiteLLM proxy."""

    def __init__(
        self,
        model_name: str,
        *,
        temperature: float = 0.0,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
        headers: Optional[Dict[str, str]] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        settings = get_settings()
        self.model_name = model_name
        self.temperature = temperature
        self.base_url = base_url or settings.litellm_base_url
        self.timeout = timeout or settings.litellm_timeout_seconds
        self.headers = headers or settings.lite_llm_headers()
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
        )

    def close(self) -> None:
        """Dispose of the underlying HTTP client if owned."""

        if self._owns_client:
            self._client.close()

    def chat(self, messages: Iterable[LLMMessage], **kwargs: Any) -> str:
        """Send chat completion request to LiteLLM and return the text response."""

        agent_config: Optional[AgentConfig] = kwargs.pop("agent_config", None)
        include_thoughts_request = bool(kwargs.pop("include_thoughts", False))
        thinking_budget_override: Optional[int] = kwargs.pop(
            "thinking_budget_override", None
        )
        generation_config_override: Optional[Dict[str, Any]] = kwargs.pop(
            "generation_config_override", None
        )

        payload: Dict[str, Any] = {
            "model": self.model_name,
            "messages": [message.as_dict() for message in messages],
            "temperature": self.temperature,
        }
        if kwargs:
            payload.update(kwargs)

        include_thoughts = include_thoughts_request
        if agent_config is not None:
            include_thoughts = include_thoughts or agent_config.include_thoughts

        extra_body = payload.setdefault("extra_body", {})
        if not isinstance(extra_body, dict):  # pragma: no cover - defensive
            extra_body = {}
            payload["extra_body"] = extra_body

        generation_config: Optional[Dict[str, Any]]
        if generation_config_override is not None:
            generation_config = generation_config_override
        elif agent_config is not None:
            generation_config = agent_config.build_generation_payload(
                max_output_override=payload.get("max_output_tokens")
                or payload.get("max_tokens"),
                thinking_budget_override=thinking_budget_override,
                include_thoughts_override=include_thoughts,
            )
        else:
            generation_config = None

        if generation_config is not None:
            if include_thoughts:
                generation_config.setdefault("responseModalities", ["TEXT"])
            extra_body.setdefault("generationConfig", generation_config)
        elif include_thoughts:
            extra_body.setdefault(
                "generationConfig", {"responseModalities": ["TEXT"]}
            )

        LOGGER.debug("liteLLM chat request", extra={"model": self.model_name})
        response = self._client.post(
            "/v1/chat/completions",
            json=payload,
            headers=self.headers,
        )
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            LOGGER.warning("LiteLLM returned no choices", extra={"model": self.model_name})
            return ""
        message = choices[0].get("message", {})
        return message.get("content", "") or ""

    def complete(self, prompt: str, **kwargs: Any) -> str:
        """Send completion request to LiteLLM and return the text response."""

        payload: Dict[str, Any] = {
            "model": self.model_name,
            "prompt": prompt,
            "temperature": self.temperature,
        }
        if kwargs:
            payload.update(kwargs)
        LOGGER.debug("liteLLM completion request", extra={"model": self.model_name})
        response = self._client.post(
            "/v1/completions",
            json=payload,
            headers=self.headers,
        )
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            LOGGER.warning(
                "LiteLLM returned no completion choices", extra={"model": self.model_name}
            )
            return ""
        text = choices[0].get("text", "")
        if isinstance(text, list):  # safety for streaming-style responses
            text = "".join(str(part) for part in text)
        return text or ""

    def __enter__(self) -> "LLMClient":  # pragma: no cover - convenience
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # pragma: no cover - convenience
        self.close()


__all__ = ["LLMClient", "LLMMessage"]
