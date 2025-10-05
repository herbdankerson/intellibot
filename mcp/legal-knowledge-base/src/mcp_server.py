"""FastAPI + FastMCP server exposing ingestion and search tools."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
from time import perf_counter
from typing import Any, Dict, List, Literal
from uuid import UUID

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, RedirectResponse
from fastmcp import FastMCP
from fastmcp.utilities.inspect import format_mcp_info
from pydantic import BaseModel, HttpUrl

from src.config import VOYAGE_MODEL_LEGAL, pg_dsn
from src.embed_voyage import embed_query
from src.ingest_web import ingest_url


logger = logging.getLogger("legal_pipeline.mcp_server")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)
logger.setLevel(logging.INFO)

MCP_PUBLIC_ENDPOINT = os.getenv("MCP_PUBLIC_ENDPOINT", "http://localhost:8100/mcp")
CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
    "CDN-Cache-Control": "no-store",
}

DEFAULT_SEARCH_TOP_K = int(os.getenv("MCP_SEARCH_TOP_K", "5"))


class IngestWebsitesInput(BaseModel):
    urls: List[HttpUrl]


def _vector_literal(values: List[float]) -> str:
    return "[" + ",".join(f"{value:.10f}" for value in values) + "]"


def _ingest_single_url(url: str) -> Dict[str, Any]:
    result = ingest_url(url)
    result["status"] = "ingested"
    return result


async def _ingest_websites(urls: List[str]) -> Dict[str, Any]:
    if not urls:
        raise ValueError("urls must contain at least one address")

    results: List[Dict[str, Any]] = []
    for raw_url in urls:
        try:
            outcome = await asyncio.to_thread(_ingest_single_url, raw_url)
            results.append(outcome)
        except RuntimeError as exc:
            results.append({"url": raw_url, "status": "skipped", "detail": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive catch for MCP
            results.append({"url": raw_url, "status": "error", "detail": str(exc)})
    return {"ingested": results}


async def ingest_websites_tool(payload: IngestWebsitesInput) -> Dict[str, Any]:
    urls = [str(url) for url in payload.urls]
    return await _ingest_websites(urls)


DOC_SQL = """
SELECT
    'docs' AS source,
    chunks.id::text AS chunk_id,
    docs.slug,
    docs.version AS document_version,
    chunks.doc_version,
    chunks.version AS chunk_version,
    chunks.section,
    chunks.idx,
    chunks.text,
    chunks.emb_legal <=> %s::vector AS distance
FROM chunks
JOIN docs ON docs.id = chunks.doc_id
ORDER BY distance
LIMIT %s;
"""

WEB_SQL = """
SELECT
    'web' AS source,
    web_chunks.id::text AS chunk_id,
    web.slug,
    web.version AS document_version,
    web_chunks.web_version,
    web_chunks.version AS chunk_version,
    web_chunks.section,
    web.url,
    web_chunks.idx,
    web_chunks.text,
    web_chunks.emb_legal <=> %s::vector AS distance
FROM web_chunks
JOIN web ON web.id = web_chunks.web_id
ORDER BY distance
LIMIT %s;
"""

DOC_CHUNK_FETCH_SQL = """
SELECT
    chunks.id::text,
    docs.slug,
    docs.version AS document_version,
    chunks.doc_version,
    chunks.version AS chunk_version,
    chunks.section,
    chunks.idx,
    chunks.text
FROM chunks
JOIN docs ON docs.id = chunks.doc_id
WHERE chunks.id = %s::uuid
LIMIT 1;
"""

WEB_CHUNK_FETCH_SQL = """
SELECT
    web_chunks.id::text,
    web.slug,
    web.url,
    web.version AS document_version,
    web_chunks.web_version,
    web_chunks.version AS chunk_version,
    web_chunks.section,
    web_chunks.idx,
    web_chunks.text
FROM web_chunks
JOIN web ON web.id = web_chunks.web_id
WHERE web_chunks.id = %s::uuid
LIMIT 1;
"""

def _search_parade(
    query: str,
    top_k: int,
    include_docs: bool,
    include_web: bool,
) -> Dict[str, Any]:
    embedding = embed_query(query, VOYAGE_MODEL_LEGAL)
    vector = _vector_literal(embedding)
    dsn = pg_dsn()
    rows: List[Dict[str, Any]] = []
    doc_limit = top_k * 2 if include_docs and include_web else top_k

    with psycopg.connect(dsn) as conn:
        if include_docs:
            with conn.cursor() as cur:
                cur.execute(DOC_SQL, (vector, doc_limit))
                for (
                    source,
                    chunk_id,
                    slug,
                    document_version,
                    doc_version,
                    chunk_version,
                    section,
                    idx,
                    text,
                    distance,
                ) in cur.fetchall():
                    rows.append(
                        {
                            "source": source,
                            "id": f"{source}:{chunk_id}",
                            "chunk_id": chunk_id,
                            "slug": slug,
                            "document_version": document_version,
                            "doc_version": doc_version,
                            "chunk_version": chunk_version,
                            "section": section,
                            "idx": idx,
                            "distance": float(distance),
                            "preview": text.replace("\n", " ")[:360],
                        }
                    )
        if include_web:
            with conn.cursor() as cur:
                cur.execute(WEB_SQL, (vector, doc_limit))
                for (
                    source,
                    chunk_id,
                    slug,
                    document_version,
                    web_version,
                    chunk_version,
                    section,
                    url,
                    idx,
                    text,
                    distance,
                ) in cur.fetchall():
                    rows.append(
                        {
                            "source": source,
                            "id": f"{source}:{chunk_id}",
                            "chunk_id": chunk_id,
                            "slug": slug,
                            "document_version": document_version,
                            "web_version": web_version,
                            "chunk_version": chunk_version,
                            "section": section,
                            "url": url,
                            "idx": idx,
                            "distance": float(distance),
                            "preview": text.replace("\n", " ")[:360],
                        }
                    )

    rows.sort(key=lambda item: item["distance"])
    return {"query": query, "results": rows[: top_k or 1]}


async def pg_search_tool(
    query: str,
    top_k: int = 5,
    source: Literal["all", "docs", "web"] = "all",
) -> Dict[str, Any]:
    if not query:
        raise ValueError("query cannot be empty")
    top_k = max(1, min(top_k, 25))
    include_docs = source in {"all", "docs"}
    include_web = source in {"all", "web"}
    if not include_docs and not include_web:
        raise ValueError("source must allow docs and/or web")

    return await asyncio.to_thread(
        _search_parade,
        query,
        top_k,
        include_docs,
        include_web,
    )


async def search_canonical(query: str) -> Dict[str, Any]:
    payload = await pg_search_tool(
        query=query,
        top_k=DEFAULT_SEARCH_TOP_K,
        source="all",
    )
    items: List[Dict[str, Any]] = []
    for row in payload["results"]:
        title_bits = [row["source"].upper(), row["slug"]]
        if "url" in row:
            title_bits.append(row["url"])
        title_bits.append(f"chunk {row['idx']}")
        title = " • ".join(str(bit) for bit in title_bits if bit)
        item = {
            "id": row["id"],
            "title": str(title),
            "snippet": str(row.get("preview") or ""),
        }
        items.append(item)
    return {"results": items}


def _parse_chunk_identifier(identifier: str) -> tuple[str, UUID]:
    try:
        source, raw_id = identifier.split(":", 1)
    except ValueError as exc:  # pragma: no cover - defensive parsing
        raise ValueError("id must be formatted as '<source>:<uuid>'") from exc

    if source not in {"docs", "web"}:
        raise ValueError("id must start with 'docs:' or 'web:'")

    try:
        chunk_uuid = UUID(raw_id)
    except ValueError as exc:  # pragma: no cover - defensive parsing
        raise ValueError("chunk id must be a valid UUID") from exc

    return source, chunk_uuid


async def fetch_chunk_tool(id: str) -> Dict[str, Any]:
    source, chunk_uuid = _parse_chunk_identifier(id)
    dsn = pg_dsn()

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        if source == "docs":
            cur.execute(DOC_CHUNK_FETCH_SQL, (str(chunk_uuid),))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"chunk not found: {id}")
            (
                chunk_id,
                slug,
                document_version,
                doc_version,
                chunk_version,
                section,
                idx,
                text,
            ) = row
            metadata = {
                "source": source,
                "slug": slug,
                "document_version": document_version,
                "doc_version": doc_version,
                "chunk_version": chunk_version,
                "section": section,
                "chunk_index": idx,
            }
            document = {
                "id": f"docs:{chunk_id}",
                "title": f"DOCS • {slug} • chunk {idx}",
                "content": text,
                "metadata": metadata,
            }
            return {"document": document}

        cur.execute(WEB_CHUNK_FETCH_SQL, (str(chunk_uuid),))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"chunk not found: {id}")
        (
            chunk_id,
            slug,
            url,
            document_version,
            web_version,
            chunk_version,
            section,
            idx,
            text,
        ) = row
        metadata = {
            "source": source,
            "slug": slug,
            "url": url,
            "document_version": document_version,
            "web_version": web_version,
            "chunk_version": chunk_version,
            "section": section,
            "chunk_index": idx,
        }
        document = {
            "id": f"web:{chunk_id}",
            "title": f"WEB • {slug} • chunk {idx}",
            "content": text,
            "metadata": metadata,
        }
        return {"document": document}
mcp = FastMCP(
    name="legal-mcp",
    instructions=(
        "Use `ingest_websites` to crawl public legal pages (statutes, guidance, etc.)"
        " and load them into ParadeDB with dedup + version tracking."
        " Use `search` when you need chunk candidates from prior ingests; the tool"
        " returns chunk metadata plus a short preview so you can cite"
        " document and chunk versions accurately."
        " Call `fetch` with an id returned by search when you need the full text"
        " for drafting or analysis."
    ),
)

ingest_websites_registration = mcp.tool(
    name="ingest_websites",
    description="Fetch one or more URLs, normalise them to Markdown, and load them into the ParadeDB `web` tables with embeddings.",
)(ingest_websites_tool)
ingest_websites_registration.parameters = IngestWebsitesInput.model_json_schema()

mcp.tool(
    name="search",
    description="Semantic search over ParadeDB documents and web ingests. Input: {query}.",
)(search_canonical)

mcp.tool(
    name="fetch",
    description="Return full chunk text and metadata for an id produced by `search`.",
)(fetch_chunk_tool)

mcp_app = mcp.http_app(path="/")


async def _mcp_entrypoint(scope, receive, send):
    """Handle gateway probes before delegating to the FastMCP transport."""

    if scope.get("type") == "http":
        method = scope.get("method")
        path = scope.get("path", "")
        headers = {k.decode(): v.decode() for k, v in scope.get("headers", [])}
        if path.startswith("/mcp/") and method == "POST":
            logger.info(
                "MCP stream init %s %s accept=%s session=%s",
                method,
                path,
                headers.get("accept"),
                headers.get("mcp-session-id"),
            )
        if path in ("", "/") and method in {"GET", "HEAD"}:
            logger.info(
                "MCP probe %s %s headers=%s",
                method,
                path,
                {key: headers.get(key) for key in ("accept", "user-agent", "mcp-session-id") if headers.get(key)},
            )
            payload = {
                "status": "ready",
                "transport": "streamable-http",
                "instructions": "POST with Accept: text/event-stream to establish MCP session.",
            }
            response = JSONResponse(payload)
            await response(scope, receive, send)
            return

    await mcp_app(scope, receive, send)

app = FastAPI(
    title="Legal Pipeline MCP",
    version="0.1.0",
    description="MCP/HTTP bridge exposing ingestion and ParadeDB semantic search tools.",
    lifespan=mcp_app.lifespan,
    redirect_slashes=False,
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = (perf_counter() - start) * 1000
        logger.exception(
            "HTTP %s %s failed after %.1f ms headers=%s",
            request.method,
            request.url.path,
            duration_ms,
            {
                "accept": request.headers.get("accept"),
                "user-agent": request.headers.get("user-agent"),
                "mcp-session-id": request.headers.get("mcp-session-id"),
            },
        )
        raise

    duration_ms = (perf_counter() - start) * 1000
    logger.info(
        "HTTP %s %s -> %s (%.1f ms) headers=%s",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
        {
            "accept": request.headers.get("accept"),
            "user-agent": request.headers.get("user-agent"),
            "mcp-session-id": request.headers.get("mcp-session-id"),
        },
    )
    return response


@app.get("/", response_model=dict)
async def root() -> Dict[str, str]:
    return {"message": "Legal Pipeline MCP server. Use /mcp for MCP transport."}


@app.get("/healthz", response_model=dict)
async def healthcheck() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/mcp")
async def mcp_probe_get() -> JSONResponse:
    payload = {
        "status": "ready",
        "transport": "streamable-http",
        "next": "/mcp/",
        "instructions": "POST with Accept: application/json, text/event-stream and include Mcp-Session-Id header to start a session.",
    }
    headers = {"Allow": "GET, HEAD, POST"}
    headers.update(CACHE_HEADERS)
    return JSONResponse(payload, headers=headers)


@app.head("/mcp")
async def mcp_probe_head() -> Response:
    headers = {"Allow": "GET, HEAD, POST"}
    headers.update(CACHE_HEADERS)
    return Response(status_code=200, headers=headers)


@app.post("/mcp")
async def mcp_post_redirect() -> RedirectResponse:
    response = RedirectResponse(url="/mcp/", status_code=307)
    response.headers.update(CACHE_HEADERS)
    response.headers["Allow"] = "GET, HEAD, POST"
    return response


def _with_transport_info(manifest: Dict[str, Any]) -> Dict[str, Any]:
    manifest = dict(manifest)
    manifest["version"] = manifest.get("version") or "1.0"
    manifest["servers"] = [
        {
            "name": mcp.name,
            "transport": {
                "type": "streamable-http",
                "endpoint": MCP_PUBLIC_ENDPOINT,
            },
            "capabilities": ["tools", "prompts", "resources"],
        }
    ]
    return manifest


def _apply_tool_overrides(
    manifest: Dict[str, Any],
    tool_name: str,
    *,
    dangerous: bool | None = None,
    schema: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Patch manifest entry with compatibility flags and schema overrides."""

    tools = manifest.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict) and tool.get("name") == tool_name:
                if dangerous is not None:
                    tool["dangerous"] = dangerous
                if schema is not None:
                    schema_payload = copy.deepcopy(schema)
                    schema_payload.setdefault("additionalProperties", False)
                    tool["inputSchema"] = schema_payload
                    tool["input_schema"] = schema_payload
                    tool["parameters"] = schema_payload
    return manifest


@app.get("/.well-known/mcp.json")
async def mcp_manifest() -> Response:
    manifest_bytes = await format_mcp_info(mcp)
    payload = _with_transport_info(json.loads(manifest_bytes))
    schema = IngestWebsitesInput.model_json_schema()
    payload = _apply_tool_overrides(
        payload,
        "ingest_websites",
        dangerous=True,
        schema=schema,
    )
    final = json.dumps(payload, separators=(",", ":"))
    headers = {"Content-Type": "application/json"}
    headers.update(CACHE_HEADERS)
    return Response(content=final, media_type="application/json", headers=headers)


@app.head("/.well-known/mcp.json")
async def mcp_manifest_head() -> Response:
    manifest_bytes = await format_mcp_info(mcp)
    payload = _with_transport_info(json.loads(manifest_bytes))
    schema = IngestWebsitesInput.model_json_schema()
    payload = _apply_tool_overrides(
        payload,
        "ingest_websites",
        dangerous=True,
        schema=schema,
    )
    final = json.dumps(payload, separators=(",", ":"))
    headers = {"content-length": str(len(final))}
    headers.update(CACHE_HEADERS)
    return Response(status_code=200, headers=headers)


@app.get("/.well-known/")
async def well_known_directory_redirect() -> RedirectResponse:
    return RedirectResponse(url="/.well-known/mcp.json", status_code=307)


@app.head("/.well-known/")
async def well_known_directory_head() -> Response:
    return Response(status_code=307, headers={"Location": "/.well-known/mcp.json"})


@app.get("/mcp/tools")
async def list_tools() -> JSONResponse:
    tools = await mcp.get_tools()
    tool_names = sorted(tool.name for tool in tools.values())
    return JSONResponse({"tools": tool_names})


app.mount("/mcp/", _mcp_entrypoint)


if __name__ == "__main__":  # pragma: no cover - convenience entrypoint
    import uvicorn

    uvicorn.run("src.mcp_server:app", host="0.0.0.0", port=8000, reload=False)
