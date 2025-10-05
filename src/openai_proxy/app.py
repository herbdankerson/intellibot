"""OpenAI-compatible FastAPI proxy that forwards calls to LiteLLM."""

from __future__ import annotations

import json
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

DEFAULT_BASE_URL = os.getenv("LITELLM_ROUTER_BASE_URL") or os.getenv(
    "LITELLM_BASE_URL", "http://litellm:4000"
)
DEFAULT_TIMEOUT = float(os.getenv("OPENAI_PROXY_TIMEOUT", os.getenv("LITELLM_TIMEOUT_SECONDS", "60")))
DEFAULT_VIRTUAL_KEY = os.getenv("LITELLM_VIRTUAL_KEY")
ROUTER_PREFIX = os.getenv("OPENAI_PROXY_PREFIX", "/v1")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialise a shared HTTPX client and tear it down on shutdown."""

    async with httpx.AsyncClient(base_url=DEFAULT_BASE_URL, timeout=DEFAULT_TIMEOUT) as client:
        app.state.litellm_client = client
        yield


app = FastAPI(title="LiteLLM OpenAI Proxy", version="0.1.0", lifespan=lifespan)


def _build_forward_headers(request: Request) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    if "content-type" in request.headers:
        headers["content-type"] = request.headers["content-type"]
    if "accept" in request.headers:
        headers["accept"] = request.headers["accept"]

    incoming_auth = request.headers.get("authorization")
    if incoming_auth:
        headers["authorization"] = incoming_auth
    elif DEFAULT_VIRTUAL_KEY:
        headers["authorization"] = f"Bearer {DEFAULT_VIRTUAL_KEY}"

    if request.headers.get("x-request-id"):
        headers["x-request-id"] = request.headers["x-request-id"]

    return headers


async def _forward_json(method: str, path: str, request: Request, payload: Dict[str, Any]) -> Response:
    client: httpx.AsyncClient = request.app.state.litellm_client
    headers = _build_forward_headers(request)
    url = f"{ROUTER_PREFIX}{path}"
    resp = await client.request(method, url, headers=headers, json=payload)
    if resp.status_code >= 400:
        detail = resp.text
        raise HTTPException(status_code=resp.status_code, detail=detail)
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
        headers={k: v for k, v in resp.headers.items() if k.lower().startswith("x-")},
    )


async def _forward_get(path: str, request: Request) -> Response:
    client: httpx.AsyncClient = request.app.state.litellm_client
    headers = _build_forward_headers(request)
    url = f"{ROUTER_PREFIX}{path}"
    resp = await client.get(url, headers=headers, params=request.query_params)
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
        headers={k: v for k, v in resp.headers.items() if k.lower().startswith("x-")},
    )


def _as_sse_payload(completion: Dict[str, Any]) -> AsyncIterator[bytes]:
    """Convert a LiteLLM JSON completion into OpenAI-style streaming SSE events."""

    response_id = completion.get("id", f"chatcmpl-{int(time.time())}")
    model = completion.get("model", "")
    created = completion.get("created", int(time.time()))
    choices = completion.get("choices", [])

    def encode(event: Dict[str, Any]) -> bytes:
        return f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")

    async def generator() -> AsyncIterator[bytes]:
        initial = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        yield encode(initial)

        content = ""
        finish_reason = "stop"
        if choices:
            first_choice = choices[0]
            message = first_choice.get("message") or {}
            content = message.get("content", "")
            finish_reason = first_choice.get("finish_reason", "stop")

        if content:
            chunk = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": content},
                        "finish_reason": None,
                    }
                ],
            }
            yield encode(chunk)

        final = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": finish_reason,
                }
            ],
        }
        yield encode(final)
        yield b"data: [DONE]\n\n"

    return generator()


@app.get("/healthz")
async def healthcheck() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models(request: Request) -> Response:
    return await _forward_get("/models", request)


@app.post("/v1/embeddings")
async def create_embeddings(request: Request) -> Response:
    payload = await request.json()
    return await _forward_json("POST", "/embeddings", request, payload)


@app.post("/v1/chat/completions")
async def create_chat_completion(request: Request) -> Response:
    payload = await request.json()
    stream = bool(payload.get("stream"))
    if stream:
        forwarded_payload = {**payload, "stream": False}
        client: httpx.AsyncClient = request.app.state.litellm_client
        headers = _build_forward_headers(request)
        url = f"{ROUTER_PREFIX}/chat/completions"
        resp = await client.post(url, headers=headers, json=forwarded_payload)
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        completion = resp.json()
        return StreamingResponse(
            _as_sse_payload(completion),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    return await _forward_json("POST", "/chat/completions", request, payload)


@app.post("/v1/completions")
async def legacy_completions(request: Request) -> Response:
    payload = await request.json()
    return await _forward_json("POST", "/completions", request, payload)


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, (str, dict)) else str(exc.detail)
    body = {"error": {"message": detail, "type": "proxy_error", "code": exc.status_code}}
    return JSONResponse(status_code=exc.status_code, content=body)
