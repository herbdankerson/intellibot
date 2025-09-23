"""Gemini client that uses LiteLLM plus the key manager for failover."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:  # pragma: no cover - integration dependency
    import litellm  # type: ignore
    from litellm.exceptions import InternalServerError, RateLimitError  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - fallback when LiteLLM unavailable
    litellm = None  # type: ignore[assignment]

    class RateLimitError(RuntimeError):
        """Fallback RateLimitError when LiteLLM is not installed."""

    class InternalServerError(RuntimeError):
        """Fallback InternalServerError when LiteLLM is not installed."""
import requests

from .gemini_key_manager import GeminiKeyManager, KeySelection, NoAvailableKeyError


DEFAULT_MODEL = "gemini/gemini-2.5-pro"


@dataclass
class GeminiFailoverClient:
    """Wrap LiteLLM calls with key rotation logic."""

    model: str = DEFAULT_MODEL
    max_attempts: int = 5
    key_manager: GeminiKeyManager | None = None

    def __post_init__(self) -> None:
        if self.key_manager is None:
            self.key_manager = GeminiKeyManager()

    def completion(self, *, messages: Iterable[Dict[str, Any]], **kwargs: Any) -> Any:
        """Execute a chat completion with automatic key failover."""

        attempt = 0
        last_error: Exception | None = None
        normalized_messages = self._normalize_messages(messages)
        base_kwargs = dict(kwargs)
        thinking_budget = base_kwargs.pop("thinking_budget", None)
        include_thoughts = base_kwargs.pop("include_thoughts", False)
        thinking_config = base_kwargs.pop("thinking_config", None)
        thinking_budget = self._normalize_thinking_budget(thinking_budget)
        extra_body = base_kwargs.setdefault("extra_body", {})
        self._apply_generation_overrides(
            extra_body,
            thinking_budget=thinking_budget,
            include_thoughts=include_thoughts,
            thinking_config=thinking_config,
        )

        while attempt < self.max_attempts:
            attempt += 1
            try:
                selection = self._acquire_key()
            except NoAvailableKeyError as exc:
                last_error = exc
                break

            call_kwargs = dict(base_kwargs)
            call_kwargs.setdefault("extra_body", extra_body)

            try:
                if litellm is not None:
                    response = litellm.completion(  # type: ignore[call-arg]
                        model=self.model,
                        messages=normalized_messages,
                        api_key=selection.secret,
                        **call_kwargs,
                    )
                    text = self._extract_text(response)
                    if text:
                        return response
                    fallback_text, raw_payload = self._direct_completion(
                        selection, normalized_messages, call_kwargs
                    )
                    if not hasattr(response, "provider_specific_fields"):
                        response.provider_specific_fields = {}
                    response.provider_specific_fields = {
                        **(response.provider_specific_fields or {}),
                        "fallback_text": fallback_text,
                        "raw_response": raw_payload,
                    }
                    return response

                fallback_text, raw_payload = self._direct_completion(
                    selection, normalized_messages, call_kwargs
                )
                return self._wrap_direct_response(fallback_text, raw_payload)
            except RateLimitError as exc:
                self.key_manager.mark_exhausted(
                    selection.model, selection.label, reason=str(exc)
                )
                last_error = exc
            except InternalServerError as exc:
                message = str(exc)
                if "overloaded" in message.lower() or "unavailable" in message.lower():
                    self.key_manager.mark_exhausted(
                        selection.model, selection.label, reason=message
                    )
                    last_error = exc
                    continue
                last_error = exc
            except Exception as exc:  # pragma: no cover - passthrough errors
                last_error = exc
                break
        if last_error is None:
            raise RuntimeError("Completion failed without surface error")
        raise last_error

    def _acquire_key(self) -> KeySelection:
        assert self.key_manager is not None
        return self.key_manager.acquire_key(self.model)

    @staticmethod
    def _normalize_messages(messages: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                normalized.append(
                    {
                        **message,
                        "content": [
                            {
                                "type": "text",
                                "text": content,
                            }
                        ],
                    }
                )
            else:
                normalized.append(message)
        return normalized

    @staticmethod
    def _extract_text(response: Any) -> Optional[str]:
        try:
            if not getattr(response, "choices", None):
                return None
            message = response.choices[0].message
            content = message.content
            if isinstance(content, str) and content.strip():
                return content
            if isinstance(content, list):
                texts = []
                for part in content:
                    if isinstance(part, dict):
                        text = part.get("text") or part.get("content")
                        if text:
                            texts.append(text)
                    elif isinstance(part, str):
                        texts.append(part)
                if texts:
                    return "\n".join(texts)
        except Exception:  # pragma: no cover - defensive
            return None
        return None

    # ------------------------------------------------------------------
    # Generation configuration helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_thinking_budget(budget: Any) -> Optional[int]:
        if budget is None:
            return None
        presets = {
            "low": 512,
            "medium": 2048,
            "high": 8192,
            "max": 32768,
            "auto": -1,
            "dynamic": -1,
            "off": 0,
            "none": 0,
        }
        if isinstance(budget, str):
            key = budget.strip().lower()
            if key in presets:
                budget = presets[key]
            else:
                try:
                    budget = int(budget)
                except ValueError as exc:  # pragma: no cover - defensive
                    raise ValueError(f"Invalid thinking budget: {budget}") from exc
        if isinstance(budget, bool):  # pragma: no cover - defensive
            budget = int(budget)
        if not isinstance(budget, int):  # pragma: no cover - defensive
            raise TypeError(f"Thinking budget must be int-str, got {type(budget)!r}")
        return budget

    def _apply_generation_overrides(
        self,
        extra_body: Dict[str, Any],
        *,
        thinking_budget: Optional[int],
        include_thoughts: bool,
        thinking_config: Optional[Dict[str, Any]],
    ) -> None:
        extra_body.setdefault("response_mime_type", "text/plain")

        generation_config: Dict[str, Any] = dict(extra_body.get("generationConfig") or {})
        merged_thinking: Dict[str, Any] = {}

        existing_thinking = generation_config.get("thinkingConfig")
        if isinstance(existing_thinking, dict):
            merged_thinking.update(existing_thinking)

        if isinstance(thinking_config, dict):
            merged_thinking.update(self._normalize_thinking_config(thinking_config))

        if thinking_budget is not None:
            merged_thinking["thinkingBudget"] = thinking_budget
        if include_thoughts:
            merged_thinking["includeThoughts"] = True

        if merged_thinking:
            generation_config["thinkingConfig"] = merged_thinking

        if generation_config:
            extra_body["generationConfig"] = generation_config

    @staticmethod
    def _normalize_thinking_config(config: Dict[str, Any]) -> Dict[str, Any]:
        normalized: Dict[str, Any] = {}
        for key, value in config.items():
            lowered = key.replace("_", "").lower()
            if lowered == "thinkingbudget":
                normalized["thinkingBudget"] = GeminiFailoverClient._normalize_thinking_budget(value)
            elif lowered == "includethoughts":
                normalized["includeThoughts"] = bool(value)
            else:
                normalized[key] = value
        return normalized

    def _direct_completion(
        self,
        selection: KeySelection,
        messages: List[Dict[str, Any]],
        call_kwargs: Dict[str, Any],
    ) -> Tuple[str, Dict[str, Any]]:
        model_name = self.model.split("/", maxsplit=1)[-1]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
        contents = []
        for message in messages:
            parts: List[Dict[str, Any]] = []
            for part in message.get("content", []):
                if isinstance(part, dict) and "text" in part:
                    parts.append({"text": part["text"]})
                elif isinstance(part, str):
                    parts.append({"text": part})
            contents.append({"role": message.get("role", "user"), "parts": parts})

        generation_config: Dict[str, Any] = {}
        if "temperature" in call_kwargs:
            generation_config["temperature"] = call_kwargs["temperature"]
        if "max_output_tokens" in call_kwargs:
            generation_config["maxOutputTokens"] = call_kwargs["max_output_tokens"]
        if "top_p" in call_kwargs:
            generation_config["topP"] = call_kwargs["top_p"]
        if "top_k" in call_kwargs:
            generation_config["topK"] = call_kwargs["top_k"]

        extra_body = call_kwargs.get("extra_body") or {}
        response_mime = extra_body.get("response_mime_type")
        if response_mime:
            generation_config["responseMimeType"] = response_mime
        generation_config.setdefault("responseModality", "TEXT")

        extra_generation = extra_body.get("generationConfig") or {}
        if extra_generation:
            generation_config.update(extra_generation)

        payload: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": generation_config,
        }

        response = requests.post(
            url,
            params={"key": selection.secret},
            json=payload,
            timeout=60,
        )
        if response.status_code == 429:
            self.key_manager.mark_exhausted(selection.model, selection.label, reason="quota")
            raise RateLimitError("Gemini quota exhausted")
        if response.status_code in (500, 503):
            message = response.text
            self.key_manager.mark_exhausted(selection.model, selection.label, reason=message)
            raise InternalServerError(message)
        response.raise_for_status()
        data = response.json()
        candidates = data.get("candidates", [])
        for candidate in candidates:
            content = candidate.get("content", {})
            parts = content.get("parts", [])
            texts = [
                part.get("text")
                for part in parts
                if isinstance(part, dict) and part.get("text")
            ]
            if texts:
                return "\n".join(texts), data
        return "", data

    @staticmethod
    def _wrap_direct_response(text: str, payload: Dict[str, Any]) -> Any:
        content: List[Any]
        if text:
            content = [{"text": text}]
        else:
            content = [{"text": ""}]
        message = SimpleNamespace(content=content)
        choice = SimpleNamespace(message=message)
        response = SimpleNamespace(
            choices=[choice],
            provider_specific_fields={
                "fallback_text": text,
                "raw_response": payload,
            },
        )
        return response


__all__ = ["GeminiFailoverClient", "DEFAULT_MODEL"]
