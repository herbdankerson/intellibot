"""LiteLLM proxy client used for planner and responder calls."""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING, Any, Dict, Iterable, Optional, Sequence, Tuple

import httpx

from ..agents import AgentConfig
from .model_router import resolve_model

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
    """Synchronous client that routes chat calls via provider adapters."""

    def __init__(
        self,
        model_name: str,
        *,
        temperature: float = 0.0,
        headers: Optional[Dict[str, str]] = None,
        run_logger: "AgentRunLogger | None" = None,
    ) -> None:
        self.model_alias = model_name
        self.temperature = temperature
        self._headers_override = dict(headers or {})
        self.run_logger = run_logger

    def close(self) -> None:  # pragma: no cover - compatibility shim
        """Retained for backwards compatibility; no resources are held."""

        return None

    def chat(self, messages: Iterable[LLMMessage], **kwargs: Any) -> str:
        """Send chat completion request and return the primary text response."""

        agent_config: Optional[AgentConfig] = kwargs.pop("agent_config", None)
        include_thoughts_request = bool(kwargs.pop("include_thoughts", False))
        thinking_budget_override: Optional[int] = kwargs.pop(
            "thinking_budget_override", None
        )
        generation_config_override: Optional[Dict[str, Any]] = kwargs.pop(
            "generation_config_override", None
        )

        routed = resolve_model(self.model_alias)
        model_record = routed.record

        request_messages = [message.as_dict() for message in messages]
        payload: Dict[str, Any] = {
            "model": model_record.name,
            "messages": request_messages,
        }

        default_params = dict(model_record.default_params)
        default_extra_body: Dict[str, Any] = {}
        extra_body_payload: Dict[str, Any] = {}
        if "extra_body" in default_params:
            candidate = default_params.pop("extra_body")
            if isinstance(candidate, dict):
                default_extra_body = dict(candidate)

        if "temperature" not in default_params and self.temperature is not None:
            default_params["temperature"] = self.temperature

        override_extra_body: Dict[str, Any] = {}
        if "extra_body" in kwargs:
            candidate = kwargs.pop("extra_body")
            if isinstance(candidate, dict):
                override_extra_body = dict(candidate)

        if default_params:
            payload.update(default_params)
        if kwargs:
            payload.update(kwargs)

        extra_body = payload.setdefault("extra_body", {})
        if not isinstance(extra_body, dict):  # pragma: no cover - defensive
            extra_body = {}
            payload["extra_body"] = extra_body
        extra_body_payload = extra_body
        for source in (default_extra_body, override_extra_body):
            for key, value in source.items():
                if (
                    isinstance(value, dict)
                    and isinstance(extra_body_payload.get(key), dict)
                ):
                    extra_body_payload[key].update(value)
                else:
                    extra_body_payload[key] = value

        include_thoughts = include_thoughts_request
        if agent_config is not None:
            include_thoughts = include_thoughts or agent_config.include_thoughts

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
            extra_body_payload.setdefault("generationConfig", generation_config)
        elif include_thoughts:
            extra_body_payload.setdefault(
                "generationConfig", {"responseModalities": ["TEXT"]}
            )

        log_payload = {
            "endpoint": "chat.completions",
            "model": model_record.name,
            "provider": model_record.provider_slug,
            "kwargs": {key: value for key, value in kwargs.items()},
            "extra_body": payload.get("extra_body"),
            "messages": _serialize_messages(request_messages),
        }

        logger = self.run_logger or get_current_run_logger()
        start = perf_counter()
        try:
            LOGGER.debug(
                "chat request",
                extra={"model": model_record.name, "provider": model_record.provider_slug},
            )
            data = routed.adapter.chat(
                route=model_record,
                payload=payload,
                headers=self._headers_override or None,
            )
            choices = data.get("choices", [])
            elapsed_ms = round((perf_counter() - start) * 1000, 2)
            if logger is not None:
                log_payload.update(
                    {
                        "elapsed_ms": elapsed_ms,
                        "response": {
                            "choices": _serialize_choices(choices),
                            "usage": data.get("usage"),
                        },
                    }
                )
                logger.log_event(
                    "llm_call",
                    log_payload,
                    tool=model_record.provider_slug,
                    status="success",
                )
            if not choices:
                LOGGER.warning(
                    "Provider returned no choices",
                    extra={"model": model_record.name, "provider": model_record.provider_slug},
                )
                return ""
            message = choices[0].get("message", {})
            return message.get("content", "") or ""
        except httpx.HTTPError as exc:
            elapsed_ms = round((perf_counter() - start) * 1000, 2)
            if logger is not None:
                log_payload.update(
                    {
                        "elapsed_ms": elapsed_ms,
                        "error": str(exc),
                    }
                )
                logger.log_event(
                    "llm_call",
                    log_payload,
                    tool=model_record.provider_slug,
                    status="error",
                )
            raise RuntimeError(f"LLM request failed: {exc}") from exc


__all__ = [
    "LLMClient",
    "LLMMessage",
    "get_current_run_logger",
    "push_run_logger",
    "reset_run_logger",
]
