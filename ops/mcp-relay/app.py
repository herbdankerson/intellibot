import asyncio
import logging
import os
from typing import AsyncIterator
from urllib.parse import urljoin

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger("mcp_relay")
logging.basicConfig(level=logging.INFO)

app = FastAPI()

RELAY_BEARER_TOKEN = os.environ.get("RELAY_BEARER_TOKEN")
UPSTREAM_SSE_URL = os.environ.get("UPSTREAM_SSE_URL")
UPSTREAM_HEALTH_URL = os.environ.get("UPSTREAM_HEALTH_URL") or "".join(
    UPSTREAM_SSE_URL.rsplit("/", 1)
) + "/health"

if not RELAY_BEARER_TOKEN:
    raise RuntimeError("RELAY_BEARER_TOKEN must be set")

if not UPSTREAM_SSE_URL:
    raise RuntimeError("UPSTREAM_SSE_URL must be set")


SESSION_ENDPOINTS: dict[str, str] = {}


def _session_key(request: Request) -> str:
    token = request.query_params.get("token")
    if token:
        return token
    auth_header = request.headers.get("Authorization")
    return auth_header or "default"


async def get_client() -> AsyncIterator[httpx.AsyncClient]:
    timeout = httpx.Timeout(None)
    async with httpx.AsyncClient(timeout=timeout) as client:
        yield client


def require_auth(request: Request) -> None:
    auth_header = request.headers.get("Authorization")
    auth_query = request.query_params.get("token")
    expected_header = f"Bearer {RELAY_BEARER_TOKEN}"

    if auth_header == expected_header:
        logger.info("authorized via header")
        return

    if auth_query == RELAY_BEARER_TOKEN:
        logger.info("authorized via query param")
        return

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.api_route("/sse", methods=["GET", "HEAD", "POST"])
async def relay_sse(
    request: Request,
    client: httpx.AsyncClient = Depends(get_client),
) -> Response:
    require_auth(request)

    logger.info("%s %s", request.method, request.url)

    if request.method == "HEAD":
        return Response(status_code=status.HTTP_200_OK)

    session_key = _session_key(request)

    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = None
        logger.info("POST body: %s", body)

        endpoint = SESSION_ENDPOINTS.get(session_key)
        if not endpoint:
            for _ in range(10):
                await asyncio.sleep(0.2)
                endpoint = SESSION_ENDPOINTS.get(session_key)
                if endpoint:
                    break
        if not endpoint:
            logger.error("No upstream endpoint registered for session %s", session_key)
            raise HTTPException(status_code=503, detail="Upstream session not ready yet")

        headers = {"Authorization": f"Bearer {RELAY_BEARER_TOKEN}"}

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as post_client:
                response = await post_client.post(endpoint, json=body, headers=headers)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("Upstream POST failed: %s", exc)
            raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text) from exc
        except httpx.HTTPError as exc:
            logger.error("Upstream POST error: %s", exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        try:
            payload = response.json()
            return JSONResponse(payload, status_code=response.status_code)
        except ValueError:
            return Response(content=response.text, status_code=response.status_code, media_type=response.headers.get("content-type", "application/json"))

    headers = {
        "Authorization": f"Bearer {RELAY_BEARER_TOKEN}",
        "Accept": "text/event-stream",
        "Cache-Control": "no-cache",
    }

    filtered_params = {
        key: value
        for key, value in request.query_params.multi_items()
        if key != "token"
    }

    request_kwargs = client.build_request(
        "GET",
        UPSTREAM_SSE_URL,
        headers=headers,
        params=filtered_params,
    )

    upstream_response = await client.send(request_kwargs, stream=True)

    try:
        upstream_response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        await upstream_response.aclose()
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text) from exc

    raw_iterator = upstream_response.aiter_raw()

    try:
        first_chunk = await raw_iterator.__anext__()
        logger.info("forwarding initial chunk %s", first_chunk[:80])
    except StopAsyncIteration:
        await upstream_response.aclose()
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except Exception as exc:
        await upstream_response.aclose()
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    await queue.put(first_chunk)

    def _maybe_register_endpoint(chunk: bytes) -> None:
        if SESSION_ENDPOINTS.get(session_key):
            return
        try:
            decoded = chunk.decode()
            if decoded.startswith("event: endpoint"):
                for line in decoded.splitlines():
                    if line.startswith("data: "):
                        endpoint_path = line[len("data: "):].strip()
                        endpoint_url = urljoin(UPSTREAM_SSE_URL, endpoint_path)
                        SESSION_ENDPOINTS[session_key] = endpoint_url
                        logger.info(
                            "registered upstream endpoint %s for session %s",
                            endpoint_url,
                            session_key,
                        )
                        break
        except Exception as exc:
            logger.warning("Could not parse endpoint from chunk: %s", exc)

    _maybe_register_endpoint(first_chunk)

    async def upstream_reader() -> None:
        try:
            async for chunk in raw_iterator:
                _maybe_register_endpoint(chunk)
                await queue.put(chunk)
        except httpx.HTTPError as exc:
            logger.warning("Upstream stream closed: %s", exc)
        finally:
            await queue.put(None)

    async def heartbeat() -> None:
        try:
            while True:
                await asyncio.sleep(20)
                await queue.put(b":\n\n")
        except asyncio.CancelledError:
            pass

    reader_task = asyncio.create_task(upstream_reader())
    heartbeat_task = asyncio.create_task(heartbeat())

    async def stream_chunks() -> AsyncIterator[bytes]:
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                yield chunk
        finally:
            reader_task.cancel()
            heartbeat_task.cancel()
            await upstream_response.aclose()

    response_headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
    }

    return StreamingResponse(
        stream_chunks(),
        media_type=upstream_response.headers.get("content-type", "text/event-stream"),
        headers=response_headers,
    )


@app.get("/")
async def root() -> dict[str, str]:
    return {"status": "relay"}


@app.get("/auth-check")
def auth_check(request: Request) -> dict[str, str]:
    require_auth(request)
    return {"status": "authorized"}
