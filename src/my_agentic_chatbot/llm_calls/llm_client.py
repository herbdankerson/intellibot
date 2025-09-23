"""LiteLLM proxy client used for planner and responder calls."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

import httpx

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

        payload: Dict[str, Any] = {
            "model": self.model_name,
            "messages": [message.as_dict() for message in messages],
            "temperature": self.temperature,
        }
        if kwargs:
            payload.update(kwargs)
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
