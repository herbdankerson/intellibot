"""Web search helper that curates, ingests, and re-ranks evidence."""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import UUID

import httpx
from readability import Document
import markdownify

from etl.tasks.model_clients import embed_with_general, summarize_with_gemini

from ..config import MCPServerConfig, get_settings
from ..ingestion.web_ingest import ingest_web_capture
from ..mcp_client.mcp_client import MCPClient, MCPToolResponse
from ..schemas import EvidenceItem, Finding, PlanTask, Requirement
from ..storage.web_work import WebWorkRecord, WebWorkTable
from ..util.text import build_snippet, extract_subject_and_location, squeeze_whitespace
from ..util.tracing import generate_run_id
from .mcp_tools import SequentialThinkingTool
from .sequential_guidance import GuidanceResult, SequentialGuidance
from .types import ToolOutcome

LOGGER = logging.getLogger(__name__)


@dataclass
class FetchResult:
    """Outcome of an HTTP or Playwright fetch."""

    url: str
    final_url: Optional[str] = None
    status_code: Optional[int] = None
    html: Optional[str] = None
    error: Optional[str] = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    via_playwright: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.html)


class FetchClient:
    """Lightweight HTTP fetcher for web pages."""

    def __init__(self, *, timeout: float = 15.0) -> None:
        self._timeout = timeout
        self._headers = {
            "User-Agent": "agentic-chatbot/1.0 (+https://github.com/herbdankerson/intellibot)",
        }

    def fetch(self, url: str) -> FetchResult:
        try:
            response = httpx.get(
                url,
                headers=self._headers,
                timeout=httpx.Timeout(self._timeout, connect=5.0),
            )
            response.raise_for_status()
            metadata = {
                "content_type": response.headers.get("content-type"),
                "encoding": response.encoding,
            }
            return FetchResult(
                url=url,
                final_url=str(response.url),
                status_code=response.status_code,
                html=response.text,
                metadata=metadata,
            )
        except Exception as exc:  # pragma: no cover - network failures depend on environment
            LOGGER.debug("HTTP fetch failed", extra={"url": url, "error": str(exc)})
            return FetchResult(url=url, error=str(exc))


class PlaywrightRenderer:
    """Playwright-backed renderer that extracts page HTML via MCP."""

    def __init__(self, client: Optional[MCPClient]) -> None:
        self._client = client

    def available(self) -> bool:
        return self._client is not None

    def render(self, url: str) -> FetchResult:
        if self._client is None:
            return FetchResult(url=url, error="playwright-mcp unavailable")
        try:
            self._client.call_tool_sync("browser_navigate", {"url": url})
            evaluation = self._client.call_tool_sync(
                "browser_evaluate",
                {"function": "() => document.documentElement.outerHTML"},
            )
            html = _extract_html_from_playwright(evaluation)
            if not html:
                return FetchResult(url=url, error="playwright returned no html")
            return FetchResult(
                url=url,
                final_url=url,
                html=html,
                via_playwright=True,
                metadata={"renderer": "playwright"},
            )
        except Exception as exc:  # pragma: no cover - depends on Playwright availability
            LOGGER.warning("Playwright render failed", extra={"url": url, "error": str(exc)})
            return FetchResult(url=url, error=str(exc))


@dataclass
class ExecutionContext:
    run_id: str
    requirement_id: Optional[str]
    task_id: Optional[str]
    iteration: int


@dataclass
class CuratedItem:
    record: WebWorkRecord
    score: float


@dataclass
class RankedCandidate:
    """Search result enriched with embeddings and similarity ranking."""

    row: Dict[str, Any]
    snippet: str
    similarity: float
    snippet_embedding: Optional[List[float]]
    query_embedding: Optional[List[float]]


class WebTool:
    """Adapter that federates SearxNG search and enriches results into the KB."""

    max_results: int = 6
    snippet_chars: int = 240

    def __init__(
        self,
        *,
        client: MCPClient | None = None,
        tool_name: str | None = None,
        work_table: Optional[WebWorkTable] = None,
        fetch_client: Optional[FetchClient] = None,
        renderer: Optional[PlaywrightRenderer] = None,
        sequential_tool: Optional[SequentialThinkingTool] = None,
        guidance: Optional[SequentialGuidance] = None,
    ) -> None:
        settings = get_settings()
        self.searx_base_url = settings.searxng_internal_url.rstrip("/") if settings.searxng_internal_url else None
        self.work_table = work_table or WebWorkTable()
        self.fetch_client = fetch_client or FetchClient()
        self.sequential_tool = sequential_tool
        self.guidance = guidance or SequentialGuidance()
        self.client = client
        self.tool_name = tool_name
        if self.client is None:
            try:
                config = settings.mcp_server("web")
            except KeyError:
                LOGGER.debug("Web MCP config missing; fallback to direct HTTP")
            else:
                self.tool_name = tool_name or self._default_tool(config)
                self.client = MCPClient(
                    base_url=config.url,
                    token=config.token,
                    server_name=f"{config.name}-client",
                )
        if renderer is not None:
            self.renderer = renderer
        else:
            self.renderer = self._build_renderer()
        self.max_search_iterations = 3
        self.guidance_window = 8
        self.min_sources = max(1, settings.acceptance_min_sources - 1)
        self.strict_min_sources = settings.acceptance_min_sources
        self.profiles: Dict[str, Dict[str, Any]] = {
            "quick": {"engines": ["duckduckgo", "google"], "num_pages": 1},
            "deep": {"engines": ["duckduckgo", "google", "bing"], "num_pages": 2},
            "code": {"engines": ["github", "stack_overflow"], "categories": ["it"]},
            "legal": {"engines": ["law_arxiv", "courtlistener"], "categories": ["law"]},
            "academic": {"engines": ["semantic_scholar", "arxiv"], "categories": ["science"]},
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(
        self,
        task: PlanTask,
        requirement: Requirement,
        *,
        limit: int | None = None,
    ) -> ToolOutcome:
        limit = limit or self.max_results
        context = self._context_from_task(task)
        base_query = str(task.inputs.get("query") or requirement.question or task.description)
        if not base_query.strip():
            return ToolOutcome(notes=["web search skipped: empty query"])

        self.work_table.cleanup_expired()
        self.work_table.reset_run(context.run_id)

        required_sources = self._required_sources(requirement)
        search_queue: List[str] = [base_query]
        seen_queries: set[str] = {base_query.lower()}
        seen_urls: set[str] = set()
        total_curated = 0
        iteration = 0
        guidance_notes: List[str] = []

        while search_queue and iteration < self.max_search_iterations:
            current_query = search_queue.pop(0)
            iteration += 1

            arguments, effective_query = self._build_arguments(
                task,
                current_query,
                max(limit, self.max_results),
                task.timeout_seconds,
            )
            rows = self._search(
                effective_query,
                max(limit * 3, self.max_results),
                task.timeout_seconds,
                arguments,
            )
            if not rows:
                continue

            query_embedding, candidates = self._prepare_candidates(effective_query, rows)
            if not candidates:
                continue

            inserted = self._insert_candidates(
                context,
                effective_query,
                candidates,
                query_embedding,
                seen_urls,
            )
            if not inserted:
                continue

            curated_this_round = self._curate_candidates(
                inserted[:limit],
                context,
                requirement,
            )
            total_curated += curated_this_round

            guidance = self._sequential_guidance(
                requirement,
                effective_query,
                [candidate.row for _, candidate in inserted[: self.guidance_window]],
            )
            note = guidance.as_note()
            if note:
                guidance_notes.append(note)

            if guidance.priority_urls:
                prioritized = self._filter_priority(inserted, guidance.priority_urls)
                if prioritized:
                    total_curated += self._curate_candidates(
                        prioritized,
                        context,
                        requirement,
                    )

            if total_curated >= required_sources:
                break

            for query in guidance.queries:
                lowered = query.lower()
                if lowered and lowered not in seen_queries:
                    search_queue.append(query)
                    seen_queries.add(lowered)

        curated_records = self.work_table.curated_items(context.run_id, limit=limit)
        evidence = [self._record_to_evidence(index, item) for index, item in enumerate(curated_records)]
        findings = [self._record_to_finding(index, item, requirement) for index, item in enumerate(curated_records)]

        notes: List[str] = []
        if guidance_notes:
            notes.extend(guidance_notes)
        notes.append(
            f"web pipeline curated {total_curated} source(s) across {iteration} search iteration(s)"
        )
        if total_curated < required_sources:
            notes.append(
                f"accepted sources below target ({total_curated}/{required_sources}); planner should continue searching"
            )
        if not evidence:
            notes.append("web curation produced no evidence")
        return ToolOutcome(evidence=evidence, findings=findings, notes=notes)

    # ------------------------------------------------------------------
    # Search + enrichment pipeline
    # ------------------------------------------------------------------

    def _search(
        self,
        query: str,
        limit: int,
        timeout: int,
        arguments: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        if self.client is None or self.tool_name is None:
            return self._direct_search(query, limit, timeout, arguments)
        try:
            response = self.client.call_tool_sync(self.tool_name, arguments)
        except Exception as exc:  # pragma: no cover - network dependent
            LOGGER.warning(
                "MCP web search failed, falling back to direct HTTP",
                extra={"query": query, "error": str(exc)},
            )
            return self._direct_search(query, limit, timeout, arguments)
        return self._parse_response(response, limit, arguments)

    def _prepare_candidates(
        self,
        query: str,
        rows: List[Dict[str, Any]],
    ) -> Tuple[Optional[List[float]], List[RankedCandidate]]:
        if not rows:
            return None, []
        snippets = [self._row_snippet(row) for row in rows]
        inputs = [query] + [snippet or query for snippet in snippets]
        try:
            embeddings = embed_with_general(inputs)
        except Exception as exc:  # pragma: no cover - embedding service may be offline
            LOGGER.warning("snippet embedding failed", extra={"query": query, "error": str(exc)})
            candidates = [
                RankedCandidate(row=row, snippet=snippet, similarity=0.0, snippet_embedding=None, query_embedding=None)
                for row, snippet in zip(rows, snippets)
            ]
            return None, candidates

        if not embeddings or len(embeddings) != len(inputs):
            LOGGER.debug(
                "unexpected embedding payload",
                extra={"expected": len(inputs), "received": len(embeddings) if embeddings else 0},
            )
            candidates = [
                RankedCandidate(row=row, snippet=snippet, similarity=0.0, snippet_embedding=None, query_embedding=None)
                for row, snippet in zip(rows, snippets)
            ]
            return None, candidates

        query_embedding = list(embeddings[0])
        ranked: List[RankedCandidate] = []
        for row, snippet, embedding in zip(rows, snippets, embeddings[1:]):
            snippet_embedding = list(embedding) if embedding is not None else None
            similarity = _cosine_similarity(query_embedding, snippet_embedding) if snippet_embedding else 0.0
            ranked.append(
                RankedCandidate(
                    row=row,
                    snippet=snippet,
                    similarity=similarity,
                    snippet_embedding=snippet_embedding,
                    query_embedding=query_embedding,
                )
            )
        ranked.sort(key=lambda candidate: candidate.similarity, reverse=True)
        return query_embedding, ranked

    def _insert_candidates(
        self,
        context: ExecutionContext,
        query: str,
        candidates: List[RankedCandidate],
        query_embedding: Optional[List[float]],
        seen_urls: set[str],
    ) -> List[Tuple[UUID, RankedCandidate]]:
        inserted: List[Tuple[UUID, RankedCandidate]] = []
        for candidate in candidates:
            url = _normalize_url(candidate.row.get("url") or candidate.row.get("source"))
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            record_id = self.work_table.insert_raw(
                run_id=context.run_id,
                requirement_id=context.requirement_id,
                task_id=context.task_id,
                query=query,
                source_url=url,
                source_title=candidate.row.get("title"),
                raw_result=candidate.row,
                snippet=candidate.snippet,
                query_embedding=query_embedding,
                snippet_embedding=candidate.snippet_embedding,
                retrieval_score=candidate.similarity,
            )
            inserted.append((record_id, candidate))
        return inserted

    def _curate_candidates(
        self,
        candidates: Iterable[Tuple[UUID, RankedCandidate]],
        context: ExecutionContext,
        requirement: Requirement,
    ) -> int:
        curated = 0
        for record_id, candidate in candidates:
            if self._curate_record(record_id, candidate.row, context, requirement):
                curated += 1
        return curated

    def _sequential_guidance(
        self,
        requirement: Requirement,
        query: str,
        rows: List[Dict[str, Any]],
    ) -> GuidanceResult:
        try:
            return self.guidance.analyse(requirement.question, query, rows)
        except Exception as exc:  # pragma: no cover - LLM failures
            LOGGER.debug("sequential guidance failed", extra={"query": query, "error": str(exc)})
            return GuidanceResult(notes=[f"guidance error: {exc}"])

    def _filter_priority(
        self,
        candidates: Iterable[Tuple[UUID, RankedCandidate]],
        priority_urls: Iterable[str],
    ) -> List[Tuple[UUID, RankedCandidate]]:
        if not priority_urls:
            return []
        target_urls = {_normalize_url(url) for url in priority_urls if url}
        prioritized: List[Tuple[UUID, RankedCandidate]] = []
        for record_id, candidate in candidates:
            url = _normalize_url(candidate.row.get("url") or candidate.row.get("source"))
            if url and url in target_urls:
                prioritized.append((record_id, candidate))
        return prioritized

    def _required_sources(self, requirement: Requirement) -> int:
        metadata_min = requirement.metadata.get("min_sources") if requirement.metadata else None
        if isinstance(metadata_min, int) and metadata_min > 0:
            return max(metadata_min, self.min_sources)
        quality = (requirement.quality_bar or "").lower()
        if any(keyword in quality for keyword in ("two", "multiple", "independent")):
            return max(self.strict_min_sources, self.min_sources)
        return self.min_sources

    def _curate_record(
        self,
        record_id: UUID,
        row: Dict[str, Any],
        context: ExecutionContext,
        requirement: Requirement,
    ) -> bool:
        url = _normalize_url(row.get("url") or row.get("source"))
        if not url:
            self.work_table.update_enrichment(
                record_id,
                fetch_status="invalid",
                metadata={"reason": "missing url"},
            )
            return False

        fetch_result = self.fetch_client.fetch(url)
        html = fetch_result.html or ""
        via_playwright = False
        if len(html) < 200 and self.renderer.available():
            pw_result = self.renderer.render(url)
            if pw_result.html:
                html = pw_result.html
                fetch_result = pw_result
                via_playwright = True

        if len(html) < 200:
            self.work_table.update_enrichment(
                record_id,
                fetch_status="failed",
                http_status=fetch_result.status_code,
                metadata={"error": fetch_result.error or "empty content"},
            )
            return False

        title, markdown, plain_text = self._html_to_markdown(url, html, row)
        if not markdown.strip():
            self.work_table.update_enrichment(
                record_id,
                fetch_status="failed",
                http_status=fetch_result.status_code,
                metadata={"error": "markdown conversion failed"},
            )
            return False

        summary = self._summarize(requirement, title, plain_text)
        authority, topicality, locality = self._compute_scores(url, plain_text, requirement)

        enrichment_meta = {
            "playwright": via_playwright,
            "final_url": fetch_result.final_url,
        }
        self.work_table.update_enrichment(
            record_id,
            fetch_status="succeeded",
            http_status=fetch_result.status_code,
            html=html if len(html) <= 500000 else html[:500000],
            markdown=markdown,
            summary=summary,
            authority_score=authority,
            topicality_score=topicality,
            locality_score=locality,
            metadata=enrichment_meta,
        )

        try:
            kb_document_id, kb_chunk_ids = ingest_web_capture(
                run_id=context.run_id,
                requirement_id=context.requirement_id,
                url=fetch_result.final_url or url,
                display_name=title or (row.get("title") or url),
                markdown=markdown,
                plain_text=plain_text,
                curated_summary=summary,
                metadata={"search_row": row},
            )
        except Exception as exc:  # pragma: no cover - ingestion failures depend on DB/LLM
            LOGGER.warning("KB ingest failed", extra={"url": url, "error": str(exc)})
            return False

        reason = f"meets acceptance gate (authority={authority:.2f}, topicality={topicality:.2f})"
        self.work_table.mark_curated(
            record_id,
            reason=reason,
            kb_document_id=kb_document_id,
            kb_chunk_ids=kb_chunk_ids,
        )
        return True

    # ------------------------------------------------------------------
    # Evidence construction
    # ------------------------------------------------------------------

    def _record_to_evidence(self, index: int, record: WebWorkRecord) -> EvidenceItem:
        score = record.score()
        source = record.source_url or f"web-curated-{record.id}"
        snippet_source = record.summary or record.snippet or record.markdown or ""
        snippet = build_snippet(snippet_source, self.snippet_chars)
        metadata = dict(record.metadata)
        metadata.update(
            {
                "kb_document_id": str(record.kb_document_id) if record.kb_document_id else None,
                "kb_chunk_ids": [str(chunk_id) for chunk_id in record.kb_chunk_ids],
                "retrieval_strategy": "web-curated",
                "authority_score": record.authority_score,
                "topicality_score": record.topicality_score,
                "locality_score": record.locality_score,
                "retrieval_score": record.retrieval_score,
            }
        )
        return EvidenceItem(
            id=f"web-curated-{index + 1}",
            source=source,
            content=snippet,
            score=score,
            metadata={k: v for k, v in metadata.items() if v is not None},
        )

    def _record_to_finding(
        self,
        index: int,
        record: WebWorkRecord,
        requirement: Requirement,
    ) -> Finding:
        confidence = _confidence_from_scores(
            record.authority_score,
            record.topicality_score,
            record.locality_score,
        )
        key = record.source_title or requirement.question
        value = record.summary or (record.markdown[:300] if record.markdown else "")
        return Finding(
            id=f"finding-web-curated-{index + 1}",
            requirement_id=requirement.id,
            key=key,
            value=value,
            confidence=confidence,
            evidence_ids=[f"web-curated-{index + 1}"],
            metadata={
                "source_url": record.source_url,
                "kb_document_id": str(record.kb_document_id) if record.kb_document_id else None,
                "retrieval_score": record.retrieval_score,
            },
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _context_from_task(self, task: PlanTask) -> ExecutionContext:
        inputs = dict(task.inputs)
        run_id = str(inputs.get("_run_id") or generate_run_id())
        requirement_id = inputs.get("_requirement_id")
        task_id = inputs.get("_task_id") or task.id
        iteration = int(inputs.get("_iteration") or 0)
        return ExecutionContext(run_id=run_id, requirement_id=requirement_id, task_id=task_id, iteration=iteration)

    def _sequential_guidance(
        self,
        task: PlanTask,
        requirement: Requirement,
        rows: List[Dict[str, Any]],
    ) -> Optional[str]:
        if self.sequential_tool is None:
            return None
        try:
            summaries = [
                f"- {row.get('title') or row.get('url')}: {squeeze_whitespace(str(row.get('snippet') or '')[:160])}"
                for row in rows[: self.max_results]
            ]
            prompt = "\n".join(summaries)
            seq_task = PlanTask(
                id=f"{task.id}-sequential",
                requirement_id=requirement.id,
                description="Reflect on web results and identify follow-ups",
                tool="agent-sequentialthinking",
                priority=task.priority,
                budget_tokens=max(task.budget_tokens, 512),
                timeout_seconds=max(task.timeout_seconds, 60),
                requires_approval=False,
                inputs={
                    "thought": f"Requirement: {requirement.question}\n\nResults:\n{prompt}",
                    "totalThoughts": 2,
                    "nextThoughtNeeded": True,
                },
            )
            thoughts = self.sequential_tool.execute(seq_task)
        except Exception as exc:  # pragma: no cover - depends on MCP runtime
            LOGGER.debug("Sequential guidance failed", extra={"task": task.id, "error": str(exc)})
            return None
        if not thoughts:
            return None
        merged = " / ".join(item.content.strip() for item in thoughts if item.content)
        merged = squeeze_whitespace(merged)
        if not merged:
            return None
        return f"sequential guidance: {merged[:380]}"

    def _html_to_markdown(
        self,
        url: str,
        html: str,
        row: Dict[str, Any],
    ) -> Tuple[str, str, str]:
        title = row.get("title") or ""
        try:
            document = Document(html)
            extracted = document.summary(html_partial=True)
            title = document.short_title() or title or url
        except Exception as exc:  # pragma: no cover - readability robustness
            LOGGER.debug("Readability failed", extra={"url": url, "error": str(exc)})
            extracted = None
        body_html = extracted or html
        body_html = _strip_noncontent_html(body_html)
        markdown = markdownify.markdownify(body_html, heading_style="ATX")
        markdown = squeeze_whitespace(markdown)
        plain_text = _markdown_to_text(markdown)
        return title or url, markdown, plain_text

    def _summarize(self, requirement: Requirement, title: str, text: str) -> str:
        snippet = text[:4000]
        prompt = f"Requirement: {requirement.question}\nSource: {title}\n\n{text[:4000]}"
        try:
            summary = summarize_with_gemini(prompt, max_length=280)
            return squeeze_whitespace(summary)
        except Exception as exc:  # pragma: no cover - Gemni availability
            LOGGER.debug("Summary generation failed", extra={"error": str(exc)})
            sentences = _first_sentences(snippet, 3)
            return squeeze_whitespace(sentences or snippet[:220])

    def _compute_scores(
        self,
        url: str,
        text: str,
        requirement: Requirement,
    ) -> Tuple[float, float, float]:
        authority = _authority_score(url)
        topicality = _topicality_score(text, requirement.question)
        locality = _locality_score(url, text, requirement)
        return authority, topicality, locality

    def _parse_response(
        self,
        response: MCPToolResponse,
        limit: int,
        arguments: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        payload = response.best_effort_payload()
        rows: List[Dict[str, Any]] = []
        if isinstance(payload, dict):
            items = payload.get("results") or payload.get("items") or payload.get("rows")
            if isinstance(items, list):
                rows = [entry for entry in items if isinstance(entry, dict)]
        elif isinstance(payload, list):
            rows = [entry for entry in payload if isinstance(entry, dict)]
        items = rows[:limit]
        return [dict(item) for item in items]

    def _row_snippet(self, row: Dict[str, Any]) -> str:
        snippet = str(row.get("snippet") or row.get("content") or "")
        if not snippet:
            snippet = str(row.get("title") or "")
        snippet = squeeze_whitespace(snippet)
        if len(snippet) > self.snippet_chars * 2:
            snippet = snippet[: self.snippet_chars * 2]
        return snippet

    def _direct_search(
        self,
        query: str,
        limit: int,
        timeout: int,
        arguments: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        if not self.searx_base_url:
            return []
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
                params[key] = value if isinstance(value, str) else ",".join(str(item) for item in value)
        engines = arguments.get("engines")
        if engines:
            params["engines"] = (
                ",".join(str(engine) for engine in engines)
                if isinstance(engines, list)
                else str(engines)
            )
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
        except Exception as exc:  # pragma: no cover - network dependent
            LOGGER.warning("Direct SearxNG search failed", extra={"query": query, "error": str(exc)})
            return []
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            return []
        return [entry for entry in results[:limit] if isinstance(entry, dict)]

    def _build_arguments(
        self,
        task: PlanTask,
        query: str,
        limit: int,
        timeout: int,
    ) -> Tuple[Dict[str, Any], str]:
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
        profile_name: Optional[str] = None
        if isinstance(task.inputs, dict):
            raw_profile = task.inputs.get("profile") or task.inputs.get("mode")
            if isinstance(raw_profile, str):
                profile_name = raw_profile.strip().lower() or None
        if profile_name and profile_name in self.profiles:
            for key, value in self.profiles[profile_name].items():
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
            ):
                if key in task.inputs and task.inputs[key] not in (None, ""):
                    arguments[key] = task.inputs[key]
        return arguments, refined_query

    def _refine_query(self, query: str, *, hint: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
        baseline = query or ""
        context = f"{baseline} {hint or ''}".strip()
        name, location = extract_subject_and_location(context)
        extras: Dict[str, Any] = {}
        lowered = context.lower()
        if name:
            terms = [f'"{name}"']
            if location:
                terms.append(location)
            if "judge" not in lowered:
                terms.append("judge")
            if any(token in lowered for token in ("biograph", "background", "education")):
                terms.append("biography education")
            if any(token in lowered for token in ("ruling", "case", "decision")):
                terms.append("notable cases")
            refined = squeeze_whitespace(" ".join(terms))
            extras.setdefault("profile", "legal")
            extras.setdefault("categories", ["law", "news"])
            extras.setdefault("engines", ["google", "bing", "duckduckgo"])
            return refined, extras
        return squeeze_whitespace(baseline), extras

    def _default_tool(self, config: MCPServerConfig) -> str:
        if config.tools:
            return config.tools[0]
        return "web_search"

    def _build_renderer(self) -> PlaywrightRenderer:
        try:
            config = get_settings().mcp_server("playwright")
        except KeyError:
            LOGGER.debug("Playwright MCP not configured; skipping renderer")
            return PlaywrightRenderer(None)
        client = MCPClient(
            base_url=config.url,
            token=config.token,
            server_name=f"{config.name}-client",
        )
        return PlaywrightRenderer(client)


def _extract_html_from_playwright(response: MCPToolResponse) -> Optional[str]:
    payload = response.best_effort_payload()
    if isinstance(payload, dict):
        candidate = payload.get("result") or payload.get("data")
        if isinstance(candidate, str):
            return candidate
    for block in response.content:
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            data = block
        if isinstance(data, str) and "<html" in data.lower():
            return data
        if isinstance(data, dict):
            candidate = data.get("result") or data.get("html")
            if isinstance(candidate, str):
                return candidate
    structured = response.structured or {}
    if isinstance(structured, dict):
        items = structured.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    html = item.get("result") or item.get("html")
                    if isinstance(html, str):
                        return html
    return None


def _normalize_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    return url.strip()


def _strip_noncontent_html(html: str) -> str:
    cleaned = re.sub(r"<(script|style)[^>]*?>.*?</\\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<!--.*?-->", " ", cleaned, flags=re.DOTALL)
    return cleaned


def _markdown_to_text(markdown: str) -> str:
    lines: List[str] = []
    for raw_line in markdown.splitlines():
        stripped = raw_line.strip()
        if stripped:
            lines.append(stripped)
    return " ".join(lines)


def _first_sentences(text: str, count: int) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(sentences[:count])


def _authority_score(url: str) -> float:
    lowered = url.lower()
    score = 0.5
    if lowered.startswith("https://"):
        score = 0.7
    if any(keyword in lowered for keyword in (".gov", ".court", "courtlistener", ".mil")):
        score = 1.0
    elif any(keyword in lowered for keyword in (".edu", "ballotpedia", "law.com", "flcourts")):
        score = max(score, 0.85)
    return score


def _topicality_score(text: str, requirement: str) -> float:
    tokens = {token.lower() for token in re.findall(r"[A-Za-z]{4,}", requirement)}
    if not tokens:
        return 0.5
    lowered = text.lower()
    matches = sum(1 for token in tokens if token in lowered)
    return min(1.0, matches / max(1, len(tokens)))


def _locality_score(url: str, text: str, requirement: Requirement) -> float:
    _, location = extract_subject_and_location(requirement.question or "")
    if not location:
        return 0.5
    lowered = location.lower()
    url_score = 1.0 if lowered in (url or "").lower() else 0.0
    text_score = 0.8 if lowered in text.lower() else 0.3
    return max(0.4, max(url_score, text_score))


def _confidence_from_scores(
    authority: Optional[float],
    topicality: Optional[float],
    locality: Optional[float],
) -> float:
    components = [value for value in (authority, topicality, locality) if value is not None]
    if not components:
        return 0.5
    base = sum(components) / len(components)
    return min(0.95, 0.4 + base * 0.5)


def _cosine_similarity(a: Optional[Sequence[float]], b: Optional[Sequence[float]]) -> float:
    if not a or not b:
        return 0.0
    if len(a) != len(b):
        return 0.0
    dot = sum(float(x) * float(y) for x, y in zip(a, b))
    norm_a = math.sqrt(sum(float(x) ** 2 for x in a))
    norm_b = math.sqrt(sum(float(y) ** 2 for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (norm_a * norm_b)))


__all__ = ["WebTool", "FetchClient", "PlaywrightRenderer"]
