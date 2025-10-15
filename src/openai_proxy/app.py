"""OpenAI-compatible FastAPI proxy that forwards calls to a configurable backend."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict

import httpx
from fastapi import FastAPI, HTTPException, Request, Response


def _normalise_prefix(prefix: str) -> str:
    cleaned = prefix.strip()
    if not cleaned:
        return ""
    if not cleaned.startswith("/"):
        cleaned = f"/{cleaned}"
    return cleaned.rstrip("/")


def _resolve_base_url() -> str:
    for env_name in ("OPENAI_PROXY_TARGET_URL", "LITELLM_ROUTER_BASE_URL", "LITELLM_BASE_URL"):
        value = os.getenv(env_name)
        if value:
            return value
    raise RuntimeError(
        "Proxy target URL is not configured; set OPENAI_PROXY_TARGET_URL (or LITELLM_ROUTER_BASE_URL/LITELLM_BASE_URL)."
    )


DEFAULT_BASE_URL = _resolve_base_url()
DEFAULT_TIMEOUT = float(os.getenv("OPENAI_PROXY_TIMEOUT", os.getenv("LITELLM_TIMEOUT_SECONDS", "60")))
DEFAULT_VIRTUAL_KEY = os.getenv("OPENAI_PROXY_DEFAULT_KEY", os.getenv("LITELLM_VIRTUAL_KEY"))
ROUTER_PREFIX = _normalise_prefix(os.getenv("OPENAI_PROXY_PREFIX", ""))

EXCLUDED_REQUEST_HEADERS = {"host", "content-length"}
HOP_BY_HOP_RESPONSE_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
SUPPORTED_HTTP_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialise a shared HTTPX client and tear it down on shutdown."""

    async with httpx.AsyncClient(base_url=DEFAULT_BASE_URL, timeout=DEFAULT_TIMEOUT) as client:
        app.state.proxy_client = client
        yield


app = FastAPI(title="OpenAI Pass-through Proxy", version="0.2.0", lifespan=lifespan)


def _build_forward_headers(request: Request) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    saw_auth = False
    for key, value in request.headers.items():
        lowered = key.lower()
        if lowered in EXCLUDED_REQUEST_HEADERS:
            continue
        if lowered == "authorization":
            saw_auth = True
        headers[key] = value

    if not saw_auth and DEFAULT_VIRTUAL_KEY:
        headers["Authorization"] = f"Bearer {DEFAULT_VIRTUAL_KEY}"

    return headers


def _filter_response_headers(headers: httpx.Headers) -> Dict[str, str]:
    filtered: Dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in HOP_BY_HOP_RESPONSE_HEADERS:
            continue
        filtered[key] = value
    return filtered


async def _forward_request(request: Request, target_path: str) -> Response:
    client: httpx.AsyncClient = request.app.state.proxy_client
    headers = _build_forward_headers(request)
    body = await request.body()
    url = f"{ROUTER_PREFIX}{target_path}"
    content = body if body else None

    resp = await client.request(
        request.method,
        url or "/",
        headers=headers,
        content=content,
        params=request.query_params,
    )

    response_headers = _filter_response_headers(resp.headers)
    media_type = resp.headers.get("content-type")
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=response_headers,
        media_type=media_type,
    )


@app.get("/healthz")
async def healthcheck() -> Dict[str, str]:
    return {"status": "ok"}


@app.api_route("/openai/verify", methods=["GET", "POST"])
@app.api_route("/v1/openai/verify", methods=["GET", "POST"])
async def openai_verify() -> Dict[str, str]:
    """Return a static OK response for compatibility checks."""

    return {"status": "ok"}


@app.api_route("/v{version}/{path:path}", methods=SUPPORTED_HTTP_METHODS)
async def proxy_versioned(version: str, path: str, request: Request) -> Response:
    if version not in {"1", "2"}:
        raise HTTPException(status_code=404, detail="Unsupported API version")

    suffix = f"/{path}" if path else ""
    target_path = f"/v{version}{suffix}"
    return await _forward_request(request, target_path)
