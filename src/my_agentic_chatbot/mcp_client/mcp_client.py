"""FastMCP client abstraction used by tool adapters."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from threading import Thread
from typing import Any, Awaitable, Callable, Dict, List, Optional

try:  # pragma: no cover - dependency is optional during certain test runs
    from fastmcp.client.client import Client as FastMCPClient
    from fastmcp.client.transports import SSETransport
    _FASTMCP_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover - fallback for environments without FastMCP
    _FASTMCP_AVAILABLE = False

    class FastMCPClient:  # type: ignore[too-few-public-methods]
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError(
                "fastmcp is not installed. Install optional dependency to use MCP tools."
            )

    class SSETransport:  # type: ignore[too-few-public-methods]
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError(
                "fastmcp is not installed. Install optional dependency to use MCP tools."
            )

LOGGER = logging.getLogger(__name__)


@dataclass
class MCPToolResponse:
    """Normalized representation of a tool call result."""

    data: Any
    structured: Dict[str, Any] | None
    content: List[str]
    is_error: bool

    def best_effort_payload(self) -> Any:
        """Return the most informative payload available."""

        if self.data is not None:
            return self.data
        if self.structured is not None:
            return self.structured
        return self.content


class MCPClient:
    """Synchronous wrapper around the FastMCP SSE client."""

    def __init__(
        self,
        base_url: str,
        *,
        token: Optional[str] = None,
        server_name: str = "postgres-mcp",
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url
        self.token = token
        self.server_name = server_name
        self.timeout = timeout

    def call_tool_sync(
        self, name: str, arguments: Dict[str, Any] | None = None
    ) -> MCPToolResponse:
        """Call an MCP tool synchronously and return the parsed response."""

        arguments = arguments or {}
        return self._run(
            lambda: self._call_tool(name=name, arguments=arguments), tool_name=name
        )

    def _run(
        self,
        coro_factory: Callable[[], Awaitable[MCPToolResponse]],
        *,
        tool_name: str,
    ) -> MCPToolResponse:
        try:
            return asyncio.run(coro_factory())
        except RuntimeError as exc:  # event loop already running
            if "event loop" not in str(exc):
                raise
            LOGGER.debug(
                "Executing MCP call on background thread",
                extra={"tool": tool_name},
            )
            result: MCPToolResponse | None = None
            error: BaseException | None = None

            def runner() -> None:
                nonlocal result, error
                try:
                    result = asyncio.run(coro_factory())
                except BaseException as run_exc:  # pragma: no cover - defensive
                    error = run_exc

            thread = Thread(target=runner, daemon=True)
            thread.start()
            thread.join()
            if error:
                raise error
            assert result is not None  # nosec B101
            return result

    async def _call_tool(self, name: str, arguments: Dict[str, Any]) -> MCPToolResponse:
        headers: Dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        transport = SSETransport(url=self.base_url, headers=headers)
        client = FastMCPClient(transport=transport, name=self.server_name, timeout=self.timeout)
        async with client:
            result = await client.call_tool(
                name=name,
                arguments=arguments,
                timeout=self.timeout,
            )
        return _normalize_result(result)


def _normalize_result(raw_result: Any) -> MCPToolResponse:
    content_blocks = getattr(raw_result, "content", None) or []
    text_blocks: List[str] = []
    for block in content_blocks:
        text = getattr(block, "text", None)
        if text is None:
            text_blocks.append(str(block))
        else:
            text_blocks.append(text)
    structured = getattr(raw_result, "structured_content", None)
    if structured is not None and isinstance(structured, list):
        structured = {"items": structured}
    return MCPToolResponse(
        data=getattr(raw_result, "data", None),
        structured=structured,
        content=text_blocks,
        is_error=bool(getattr(raw_result, "is_error", False)),
    )


__all__ = ["MCPClient", "MCPToolResponse"]
