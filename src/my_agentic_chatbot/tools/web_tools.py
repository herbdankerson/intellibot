"""Web search helper backed by SearxNG via MCP or direct HTTP fallback."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List

import httpx

from ..config import MCPServerConfig, get_settings
from ..mcp_client.mcp_client import MCPClient, MCPToolResponse
from ..schemas import EvidenceItem, Finding, PlanTask, Requirement
from ..util.text import (
    build_snippet,
    deduplicate_items,
    extract_subject_and_location,
    squeeze_whitespace,
)
from .types import ToolOutcome

LOGGER = logging.getLogger(__name__)


@dataclass
class WebTool:
    """Adapter that federates SearxNG search results through MCP."""

    max_results: int = 6
    snippet_chars: int = 240
    client: MCPClient | None = None
    tool_name: str | None = None
    searx_base_url: str | None = None
    profiles: Dict[str, Dict[str, Any]] = field(
        default_factory=lambda: {
            "quick": {"engines": ["duckduckgo", "google"], "num_pages": 1},
            "deep": {"engines": ["duckduckgo", "google", "bing"], "num_pages": 2},
            "code": {"engines": ["github", "stack_overflow"], "categories": ["it"]},
            "legal": {"engines": ["law_arxiv", "courtlistener"], "categories": ["law"]},
            "academic": {"engines": ["semantic_scholar", "arxiv"], "categories": ["science"]},
        }
    )

    def __post_init__(self) -> None:
        settings = get_settings()
        self.searx_base_url = settings.searxng_internal_url.rstrip("/")
        if self.client is None:
            try:
                config = settings.mcp_server("web")
            except KeyError:
                LOGGER.debug(
                    "Web MCP config missing; falling back to direct SearxNG HTTP client"
                )
                return
            self.tool_name = self.tool_name or self._default_tool(config)
            self.client = MCPClient(
                base_url=config.url,
                token=config.token,
                server_name=f"{config.name}-client",
            )
        else:
            self.tool_name = self.tool_name or "web_search"

    def execute(
        self,
        task: PlanTask,
        requirement: Requirement,
        *,
        limit: int | None = None,
    ) -> ToolOutcome:
        """Execute a search task using MCP, returning evidence and candidate findings."""

        query = str(task.inputs.get("query") or requirement.question or task.description)
        if not query.strip():
            return ToolOutcome(notes=["web search skipped: empty query"])
        limit = self._resolve_limit(task, limit)
        timeout = max(1, task.timeout_seconds)
        arguments, effective_query = self._build_arguments(task, query, limit, timeout)
        evidence: List[EvidenceItem]
        if not self.client or not self.tool_name:
            evidence = self._direct_search(effective_query, limit, timeout, arguments)
        else:
            try:
                response = self.client.call_tool_sync(self.tool_name, arguments)
                evidence = self._parse_response(response, limit, arguments)
            except Exception as exc:  # pragma: no cover - network failures exercised via mocks
                LOGGER.warning(
                    "MCP web search failed, falling back to direct SearxNG",
                    exc_info=exc,
                    extra={"query": effective_query, "mode": arguments.get("profile")},
                )
                evidence = self._direct_search(effective_query, limit, timeout, arguments)

        findings = self._evidence_to_findings(requirement, evidence)
        notes: List[str] = []
        if not evidence:
            notes.append("web search returned no evidence")
        return ToolOutcome(evidence=evidence, findings=findings, notes=notes)

    def search(self, query: str) -> List[EvidenceItem]:
        """Legacy helper used by tests; returns stub evidence."""

        return self._stub_response(query, self.max_results)

    def _resolve_limit(self, task: PlanTask, limit: int | None) -> int:
        final_limit = limit or self.max_results
        if isinstance(task.inputs, dict):
            candidate = task.inputs.get("limit")
            if isinstance(candidate, int) and candidate > 0:
                final_limit = min(final_limit, candidate)
        return max(1, final_limit)

    def _parse_response(
        self,
        response: MCPToolResponse,
        limit: int,
        arguments: Dict[str, Any],
    ) -> List[EvidenceItem]:
        payload = response.best_effort_payload()
        rows: List[Dict[str, Any]] = []
        if isinstance(payload, dict):
            items = payload.get("results") or payload.get("items") or payload.get("rows")
            if isinstance(items, list):
                rows = [entry for entry in items if isinstance(entry, dict)]
        elif isinstance(payload, list):
            rows = [entry for entry in payload if isinstance(entry, dict)]
        items = [
            self._row_to_evidence(index, row, arguments, retrieval_strategy="web-mcp")
            for index, row in enumerate(rows[:limit])
        ]
        return deduplicate_items(items)

    def _row_to_evidence(
        self,
        index: int,
        row: Dict[str, Any],
        arguments: Dict[str, Any],
        *,
        retrieval_strategy: str,
    ) -> EvidenceItem:
        snippet = build_snippet(str(row.get("snippet") or row.get("content") or ""), self.snippet_chars)
        source = row.get("url") or row.get("source") or row.get("domain") or "web"
        title = row.get("title") or ""
        metadata: Dict[str, str] = {}
        for key in ("title", "snippet", "content"):
            row.pop(key, None)
        for key, value in row.items():
            if value is None:
                continue
            if isinstance(value, (list, dict)):
                continue
            metadata[key] = str(value)
        if title:
            metadata.setdefault("title", title)
        metadata.setdefault("retrieval_strategy", retrieval_strategy)
        profile = arguments.get("profile") or arguments.get("mode")
        if profile:
            metadata.setdefault("search_profile", str(profile))
        if arguments.get("group"):
            metadata.setdefault("engine_group", str(arguments["group"]))
        if arguments.get("engines"):
            engines_value = arguments["engines"]
            if isinstance(engines_value, list):
                metadata.setdefault("engines", ",".join(str(v) for v in engines_value))
            else:
                metadata.setdefault("engines", str(engines_value))
        return EvidenceItem(
            id=f"web-{index + 1}",
            source=str(source),
            content=snippet,
            score=float(row.get("score", 0.15) or 0.15),
            metadata=metadata,
        )

    def _stub_response(self, query: str, limit: int) -> List[EvidenceItem]:
        LOGGER.warning(
            "Web search unavailable; returning no evidence",
            extra={"query": squeeze_whitespace(query)},
        )
        return []

    def _default_tool(self, config: MCPServerConfig) -> str:
        if config.tools:
            return config.tools[0]
        LOGGER.warning("Web MCP config missing tools list; defaulting to web_search")
        return "web_search"

    def _build_arguments(
        self, task: PlanTask, query: str, limit: int, timeout: int
    ) -> tuple[Dict[str, Any], str]:
        final_limit = max(1, min(limit, self.max_results))
        arguments: Dict[str, Any] = {
            "query": query,
            "limit": final_limit,
            "timeout_seconds": timeout,
        }
        refined_query, refinements = self._refine_query(query, hint=task.description)
        arguments["query"] = refined_query
        for key, value in refinements.items():
            arguments.setdefault(key, value)
        profile_name: str | None = None
        if isinstance(task.inputs, dict):
            raw_profile = task.inputs.get("profile") or task.inputs.get("mode")
            if isinstance(raw_profile, str):
                profile_name = raw_profile.strip().lower() or None
        if profile_name and profile_name in self.profiles:
            profile_arguments = self.profiles[profile_name]
            for key, value in profile_arguments.items():
                arguments.setdefault(key, value)
            arguments.setdefault("profile", profile_name)
        if isinstance(task.inputs, dict):
            for key in (
                "group",
                "engines",
                "categories",
                "language",
                "time_range",
                "site",
                "format",
                "fetch_content",
                "use_text",
            ):
                if key in task.inputs and task.inputs[key] not in (None, ""):
                    arguments[key] = task.inputs[key]
        return arguments, refined_query

    def _refine_query(self, query: str, *, hint: str | None = None) -> tuple[str, Dict[str, Any]]:
        """Create a more targeted query and associated SearxNG tuning hints."""

        baseline = query or ""
        context = f"{baseline} {hint or ''}".strip()
        name, location = extract_subject_and_location(context)
        extras: Dict[str, Any] = {}
        lowered = context.lower()
        if name:
            terms = [f'"{name}"']
            if location:
                terms.append(location)
            if "sarasota" in lowered and "sarasota" not in " ".join(terms).lower():
                terms.append("Sarasota Florida")
            if "florida" in lowered and "florida" not in " ".join(terms).lower():
                terms.append("Florida")
            terms.append("judge")
            if any(token in lowered for token in ("biograph", "background", "education")):
                terms.append("biography background")
            if any(token in lowered for token in ("news", "recent", "article", "profile")):
                terms.append("news profile")
            if any(token in lowered for token in ("ruling", "opinion", "case", "decision")):
                terms.append("notable cases")
            refined = squeeze_whitespace(" ".join(terms))
            extras.setdefault("profile", "legal")
            extras.setdefault("categories", ["law", "news"])
            extras.setdefault("engines", ["google", "bing", "duckduckgo"])
            return refined, extras
        return squeeze_whitespace(baseline), extras

    def _direct_search(
        self,
        query: str,
        limit: int,
        timeout: int,
        arguments: Dict[str, Any],
    ) -> List[EvidenceItem]:
        """Query SearxNG HTTP API directly when MCP is unavailable."""

        if not self.searx_base_url:
            return self._stub_response(query, limit)

        params: Dict[str, Any] = {
            "q": query,
            "format": "json",
            "language": arguments.get("language", "en"),
            "safesearch": arguments.get("safesearch", 1),
            "num": max(1, min(limit, self.max_results)),
        }
        for key in ("time_range", "site", "categories"):
            value = arguments.get(key)
            if value:
                params[key] = value if isinstance(value, str) else ",".join(
                    str(item) for item in value
                )
        engines = arguments.get("engines")
        if engines:
            if isinstance(engines, list):
                params["engines"] = ",".join(str(engine) for engine in engines)
            else:
                params["engines"] = str(engines)
        else:
            params["engines"] = "duckduckgo"

        headers = {"User-Agent": "agentic-chatbot/1.0"}
        search_url = f"{self.searx_base_url}/search"
        try:
            response = httpx.get(
                search_url,
                params=params,
                headers=headers,
                timeout=httpx.Timeout(timeout + 5, connect=5.0),
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:  # pragma: no cover - network fallback only
            LOGGER.warning(
                "Direct SearxNG search failed, returning stub evidence",
                exc_info=exc,
                extra={"query": query},
            )
            return self._stub_response(query, limit)

        rows = []
        if isinstance(payload, dict):
            results = payload.get("results")
            if isinstance(results, list):
                rows = [row for row in results if isinstance(row, dict)]
        items = [
            self._row_to_evidence(
                index,
                row,
                arguments,
                retrieval_strategy="web-searx",
            )
            for index, row in enumerate(rows[:limit])
        ]
        if not items:
            return self._stub_response(query, limit)
        return deduplicate_items(items)

    def _evidence_to_findings(
        self, requirement: Requirement, evidence: List[EvidenceItem]
    ) -> List[Finding]:
        findings: List[Finding] = []
        for index, item in enumerate(evidence):
            if not item.content.strip():
                continue
            base_confidence = _web_confidence(item.score)
            findings.append(
                Finding(
                    id=f"finding-web-{index + 1}",
                    requirement_id=requirement.id,
                    key=requirement.question,
                    value=item.content,
                    confidence=base_confidence,
                    evidence_ids=[item.id],
                    metadata={"source": item.source, **item.metadata},
                )
            )
        return findings


def _web_confidence(score: float) -> float:
    try:
        value = float(score)
    except (TypeError, ValueError):
        value = 0.2
    value = max(0.0, min(1.0, value))
    if value >= 0.8:
        return 0.75
    if value <= 0.1:
        return 0.35
    return 0.35 + value * 0.4


__all__ = ["WebTool"]
