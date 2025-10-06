"""Database search utilities used by the orchestrator."""

from __future__ import annotations

import ast
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

from ..config import MCPServerConfig, get_settings
from ..mcp_client.mcp_client import MCPClient, MCPToolResponse
from ..schemas import EvidenceItem, PlanTask
from ..util.text import build_snippet, deduplicate_items

LOGGER = logging.getLogger(__name__)


@dataclass
class DatabaseTool:
    """Adapter that queries the Postgres MCP server."""

    max_results: int = 5
    snippet_chars: int = 320
    client: MCPClient | None = None
    tool_name: str | None = None

    def __post_init__(self) -> None:
        if self.client is None:
            settings = get_settings()
            config = settings.mcp_server("postgres")
            self.tool_name = self.tool_name or self._default_tool(config)
            self.client = MCPClient(
                base_url=config.url,
                token=config.token,
                server_name=f"{config.name}-client",
            )
        else:
            self.tool_name = self.tool_name or "execute_sql"

    def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        timeout_seconds: int | None = None,
    ) -> List[EvidenceItem]:
        """Return evidence items for the provided natural language query."""

        if not query.strip():
            return []
        limit = min(limit or self.max_results, self.max_results)
        if not self.client or not self.tool_name:
            raise RuntimeError("Database tool is not configured with an MCP client")

        sql = self._build_search_sql(query, limit)
        arguments: Dict[str, Any] = {"sql": sql}
        response = self.client.call_tool_sync(self.tool_name, arguments)
        rows = self._extract_rows(response)
        items = [self._row_to_evidence(index, row) for index, row in enumerate(rows[:limit])]
        if not items and response.content:
            items = self._fallback_from_content(response.content, limit)
        return deduplicate_items(items)[:limit]

    def execute(self, task: PlanTask, *, limit: int | None = None) -> List[EvidenceItem]:
        """Run the MCP-backed search based on the plan task inputs."""

        query = str(task.inputs.get("query") or task.description)
        requested_limit = task.inputs.get("limit") if isinstance(task.inputs, dict) else None
        final_limit = limit or self.max_results
        if isinstance(requested_limit, int) and requested_limit > 0:
            final_limit = min(final_limit, requested_limit)
        timeout = task.timeout_seconds
        return self.search(query, limit=final_limit, timeout_seconds=timeout)

    def _default_tool(self, config: MCPServerConfig) -> str:
        if config.tools:
            return config.tools[0]
        LOGGER.warning("Postgres MCP config missing tools list; defaulting to execute_sql")
        return "execute_sql"

    def _extract_rows(self, response: MCPToolResponse) -> List[Dict[str, Any]]:
        payload = response.best_effort_payload()
        rows: List[Dict[str, Any]] = []
        if isinstance(payload, dict):
            candidate = payload.get("result")
            if isinstance(candidate, list):
                rows = self._parse_text_blocks(candidate)
            else:
                for key in ("rows", "items", "results", "data"):
                    candidate = payload.get(key)
                    if isinstance(candidate, list):
                        rows = [entry for entry in candidate if isinstance(entry, dict)]
                        if rows:
                            break
        elif isinstance(payload, list):
            rows = [entry for entry in payload if isinstance(entry, dict)]
        else:
            LOGGER.debug("Unexpected MCP payload type", extra={"type": type(payload).__name__})
        if not rows and response.content:
            rows = self._parse_text_blocks(response.content)
        if (
            len(rows) == 1
            and isinstance(rows[0], dict)
            and isinstance(rows[0].get("results"), list)
        ):
            rows = [entry for entry in rows[0]["results"] if isinstance(entry, dict)]
        return rows

    def _parse_text_blocks(self, blocks: Iterable[Any]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for block in blocks:
            text = block
            if isinstance(block, dict) and "text" in block:
                text = block.get("text")
            if not isinstance(text, str):
                continue
            try:
                data = json.loads(text)
            except (TypeError, json.JSONDecodeError):
                try:
                    data = ast.literal_eval(text)
                except (SyntaxError, ValueError):
                    continue
            if isinstance(data, list):
                rows.extend(entry for entry in data if isinstance(entry, dict))
            elif isinstance(data, dict):
                rows.append(data)
        return rows

    def _row_to_evidence(self, index: int, row: Dict[str, Any]) -> EvidenceItem:
        raw_content = (
            row.get("snippet")
            or row.get("content")
            or row.get("summary")
            or row.get("text")
            or ""
        )
        snippet = build_snippet(str(raw_content), self.snippet_chars)
        source = (
            row.get("source")
            or row.get("source_uri")
            or row.get("uri")
            or row.get("document_id")
            or row.get("id")
            or "postgres-mcp"
        )
        score_value = row.get("score") or row.get("rank") or row.get("bm25") or 0.0
        try:
            score = float(score_value)
        except (TypeError, ValueError):
            score = 0.0
        metadata: Dict[str, str] = {}
        for key in (
            "display_name",
            "document_title",
            "source_type",
            "document_summary",
        ):
            value = row.get(key)
            if value:
                metadata[key] = str(value)
        for key in ("ingest_item_id", "document_id"):
            value = row.get(key)
            if value:
                metadata[key] = str(value)

        json_fields = {
            "chunk_metadata": row.get("metadata"),
            "ingest_metadata": row.get("ingest_metadata"),
            "document_metadata": row.get("document_metadata"),
        }
        for key, value in json_fields.items():
            if isinstance(value, dict) and value:
                metadata[key] = json.dumps(value, sort_keys=True)
        if "retrieval_strategy" not in metadata:
            metadata["retrieval_strategy"] = "postgres-mcp"
        return EvidenceItem(
            id=f"db-{index + 1}",
            source=str(source),
            content=snippet,
            score=score,
            metadata=metadata,
        )

    def _build_search_sql(self, query: str, limit: int) -> str:
        sanitized = query.replace("'", "''")
        limit = max(1, min(limit, self.max_results))
        candidate_limit = max(limit * 4, limit)
        sql = f"""
WITH
    search_query AS (
        SELECT NULLIF(websearch_to_tsquery('simple', '{sanitized}'), to_tsquery('simple', '')) AS query
    ),
    ranked_chunks AS (
        SELECT
            c.id,
            c.text,
            c.summary,
            c.metadata,
            c.ingest_item_id,
            c.document_id,
            ts_rank_cd(c.tsv, sq.query) AS rank,
            ts_headline('simple', c.text, sq.query, 'MaxFragments=2, MaxWords=32, ShortWord=3') AS snippet
        FROM kb.chunks AS c
        CROSS JOIN search_query AS sq
        WHERE sq.query IS NOT NULL AND c.tsv @@ sq.query
        ORDER BY rank DESC
        LIMIT {candidate_limit}
    ),
    enriched AS (
        SELECT
            rc.id,
            rc.rank,
            rc.snippet,
            rc.summary,
            rc.text,
            rc.metadata,
            rc.ingest_item_id,
            rc.document_id,
            i.display_name,
            i.source_uri,
            i.source_type,
            i.document_summary,
            i.metadata AS ingest_metadata,
            d.title AS document_title,
            d.metadata AS document_metadata
        FROM ranked_chunks AS rc
        JOIN kb.ingest_items AS i ON i.id = rc.ingest_item_id
        LEFT JOIN kb.documents AS d ON d.id = rc.document_id
        ORDER BY rc.rank DESC
        LIMIT {limit}
    )
SELECT COALESCE(
    json_agg(
        json_build_object(
            'id', enriched.id::text,
            'rank', enriched.rank,
            'snippet', enriched.snippet,
            'summary', enriched.summary,
            'text', enriched.text,
            'metadata', enriched.metadata,
            'ingest_item_id', enriched.ingest_item_id::text,
            'document_id', enriched.document_id::text,
            'display_name', enriched.display_name,
            'source_uri', enriched.source_uri,
            'source_type', enriched.source_type,
            'document_summary', enriched.document_summary,
            'ingest_metadata', enriched.ingest_metadata,
            'document_title', enriched.document_title,
            'document_metadata', enriched.document_metadata
        )
    ),
    '[]'::json
) AS results
FROM enriched;
"""
        return sql

    def _fallback_from_content(self, content: Iterable[str], limit: int) -> List[EvidenceItem]:
        items: List[EvidenceItem] = []
        for index, block in enumerate(content):
            if index >= limit:
                break
            snippet = build_snippet(str(block), self.snippet_chars)
            items.append(
                EvidenceItem(
                    id=f"db-{index + 1}",
                    source="postgres-mcp",
                    content=snippet,
                    score=0.0,
                    metadata={"retrieval_strategy": "text-fallback"},
                )
            )
        return items


__all__ = ["DatabaseTool"]
