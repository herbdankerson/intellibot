"""LiteLLM proxy client used for planner and responder calls."""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING, Any, Dict, Iterable, Optional, Sequence, Tuple

import httpx

from ..agents import AgentConfig
from ..config import get_settings

if TYPE_CHECKING:  # pragma: no cover - typing helper
    from ..run_logging import AgentRunLogger

LOGGER = logging.getLogger(__name__)


_CURRENT_RUN_LOGGER: ContextVar["AgentRunLogger | None"] = ContextVar(
    "current_run_logger",
    default=None,
)


def push_run_logger(logger: "AgentRunLogger | None") -> Token["AgentRunLogger | None"]:
    """Bind a run logger to the current execution context."""

    return _CURRENT_RUN_LOGGER.set(logger)


def reset_run_logger(token: Token["AgentRunLogger | None"]) -> None:
    """Restore the previously bound run logger."""

    _CURRENT_RUN_LOGGER.reset(token)


def get_current_run_logger() -> "AgentRunLogger | None":
    """Return the run logger for the current context if available."""

    return _CURRENT_RUN_LOGGER.get()


def _truncate_text(value: str, limit: int = 2000) -> Tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return value[: limit - 1] + "…", True


def _serialize_messages(messages: Sequence[Dict[str, str]]) -> Sequence[Dict[str, object]]:
    serialized: list[Dict[str, object]] = []
    for entry in messages:
        content = str(entry.get("content") or "")
        truncated, did_truncate = _truncate_text(content)
        serialized.append(
            {
                "role": entry.get("role"),
                "content": truncated,
                "truncated": did_truncate,
            }
        )
    return serialized


def _serialize_choices(data: Optional[Sequence[Dict[str, Any]]]) -> Sequence[Dict[str, object]]:
    output: list[Dict[str, object]] = []
    if not data:
        return output
    for item in data[:3]:
        message = item.get("message") if isinstance(item, dict) else {}
        content = "" if message is None else str(message.get("content", ""))
        truncated, did_truncate = _truncate_text(content)
        output.append(
            {
                "finish_reason": item.get("finish_reason"),
                "index": item.get("index"),
                "content": truncated,
                "truncated": did_truncate,
            }
        )
    return output


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
        run_logger: "AgentRunLogger | None" = None,
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
        self.run_logger = run_logger

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

        request_messages = [message.as_dict() for message in messages]
        payload: Dict[str, Any] = {
            "model": self.model_name,
            "messages": request_messages,
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

        log_payload = {
            "endpoint": "chat.completions",
            "model": self.model_name,
            "temperature": self.temperature,
            "kwargs": {key: value for key, value in kwargs.items()},
            "extra_body": payload.get("extra_body"),
            "messages": _serialize_messages(request_messages),
        }

        logger = self.run_logger or get_current_run_logger()
        start = perf_counter()
        try:
            LOGGER.debug("liteLLM chat request", extra={"model": self.model_name})
            response = self._client.post(
                "/v1/chat/completions",
                json=payload,
                headers=self.headers,
            )
            response.raise_for_status()
            data = response.json()
            choices = data.get("choices", [])
            if logger is not None:
                log_payload.update(
                    {
                        "status_code": response.status_code,
                        "elapsed_ms": round((perf_counter() - start) * 1000, 2),
                        "response": {
                            "choices": _serialize_choices(choices),
                            "usage": data.get("usage"),
                        },
                    }
                )
                logger.log_event(
                    "llm_call",
                    log_payload,
                    tool="liteLLM",
                    status="success",
                )
            if not choices:
                LOGGER.warning("LiteLLM returned no choices", extra={"model": self.model_name})
                return ""
            message = choices[0].get("message", {})
            return message.get("content", "") or ""
        except Exception as exc:
            if logger is not None:
                log_payload.update(
                    {
                        "elapsed_ms": round((perf_counter() - start) * 1000, 2),
                        "error": str(exc),
                    }
                )
                logger.log_event(
                    "llm_call",
                    log_payload,
                    tool="liteLLM",
                    status="error",
                )
            raise

    def complete(self, prompt: str, **kwargs: Any) -> str:
        """Send completion request to LiteLLM and return the text response."""

        payload: Dict[str, Any] = {
            "model": self.model_name,
            "prompt": prompt,
            "temperature": self.temperature,
        }
        if kwargs:
            payload.update(kwargs)
        logger = self.run_logger or get_current_run_logger()
        prompt_preview, prompt_truncated = _truncate_text(str(prompt))
        log_payload = {
            "endpoint": "completions",
            "model": self.model_name,
            "temperature": self.temperature,
            "prompt": prompt_preview,
            "prompt_truncated": prompt_truncated,
            "kwargs": {key: value for key, value in kwargs.items()},
        }
        start = perf_counter()
        try:
            LOGGER.debug("liteLLM completion request", extra={"model": self.model_name})
            response = self._client.post(
                "/v1/completions",
                json=payload,
                headers=self.headers,
            )
            response.raise_for_status()
            data = response.json()
            choices = data.get("choices", [])
            if logger is not None:
                preview = ""
                truncated = False
                if choices:
                    text = choices[0].get("text", "")
                    if isinstance(text, list):
                        text = "".join(str(part) for part in text)
                    preview, truncated = _truncate_text(str(text))
                log_payload.update(
                    {
                        "status_code": response.status_code,
                        "elapsed_ms": round((perf_counter() - start) * 1000, 2),
                        "response": {
                            "choices": [
                                {
                                    "index": choices[0].get("index") if choices else 0,
                                    "text": preview,
                                    "truncated": truncated,
                                }
                            ]
                            if choices
                            else [],
                            "usage": data.get("usage"),
                        },
                    }
                )
                logger.log_event(
                    "llm_call",
                    log_payload,
                    tool="liteLLM",
                    status="success",
                )
            if not choices:
                LOGGER.warning(
                    "LiteLLM returned no completion choices", extra={"model": self.model_name}
                )
                return ""
            text = choices[0].get("text", "")
            if isinstance(text, list):  # safety for streaming-style responses
                text = "".join(str(part) for part in text)
            return text or ""
        except Exception as exc:
            if logger is not None:
                log_payload.update(
                    {
                        "elapsed_ms": round((perf_counter() - start) * 1000, 2),
                        "error": str(exc),
                    }
                )
                logger.log_event(
                    "llm_call",
                    log_payload,
                    tool="liteLLM",
                    status="error",
                )
            raise

    def __enter__(self) -> "LLMClient":  # pragma: no cover - convenience
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # pragma: no cover - convenience
        self.close()


__all__ = [
    "LLMClient",
    "LLMMessage",
    "get_current_run_logger",
    "push_run_logger",
    "reset_run_logger",
]
