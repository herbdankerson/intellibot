"""Provider adapter registry and model resolution utilities."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, Optional

import httpx

from ..config import get_settings
from ..storage.config_repo import ModelRecord, get_registry


class ProviderAdapter:
    """Abstract provider adapter."""

    def chat(
        self,
        *,
        route: ModelRecord,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError


class LiteLLMAdapter(ProviderAdapter):
    """Adapter that forwards chat calls to a LiteLLM-compatible endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout: float,
        default_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._default_headers = dict(default_headers or {})

    def _build_headers(self, overrides: Optional[Dict[str, str]]) -> Dict[str, str]:
        headers = dict(self._default_headers)
        if overrides:
            headers.update(overrides)
        return headers

    def chat(
        self,
        *,
        route: ModelRecord,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> Dict[str, Any]:
        base_url = (route.endpoint or self._base_url).rstrip("/")
        merged_headers = self._build_headers(headers)
        if http_client is not None:
            response = http_client.post("/v1/chat/completions", json=payload, headers=merged_headers)
        else:
            with httpx.Client(base_url=base_url, timeout=self._timeout) as client:
                response = client.post("/v1/chat/completions", json=payload, headers=merged_headers)
        response.raise_for_status()
        return response.json()


@dataclass(frozen=True)
class RoutedModel:
    """Resolved model metadata plus provider adapter."""

    record: ModelRecord
    adapter: ProviderAdapter


def resolve_model(alias: str) -> RoutedModel:
    """Return the provider adapter and model record for ``alias``."""

    registry = get_registry()
    record = registry.require_model(alias)
    adapter = get_provider_adapter(record.provider_slug)
    return RoutedModel(record=record, adapter=adapter)


@lru_cache(maxsize=8)
def get_provider_adapter(slug: str) -> ProviderAdapter:
    """Return the adapter associated with a provider slug."""

    settings = get_settings()
    adapters: Dict[str, ProviderAdapter] = {
        "litellm": LiteLLMAdapter(
            base_url=settings.litellm_base_url,
            timeout=settings.litellm_timeout_seconds,
            default_headers=settings.lite_llm_headers(),
        ),
    }
    if slug not in adapters:
        raise RuntimeError(f"No provider adapter registered for '{slug}'")
    return adapters[slug]


__all__ = [
    "LiteLLMAdapter",
    "ProviderAdapter",
    "RoutedModel",
    "get_provider_adapter",
    "resolve_model",
]
