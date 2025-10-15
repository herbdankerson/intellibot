"""Orchestration helpers for the ingestion API."""

from __future__ import annotations

from typing import Dict, Optional

from etl.flows.flow_document_intake import document_intake_flow
from etl.tasks.intake_models import FlowReport, IngestItem

from ..schemas import IngestJobStatus


class IngestionService:
    """Synchronous wrapper around the Prefect ingestion flow.

    The real deployment will submit flow runs to a Prefect work queue. Until
    that glue is in place we execute the flow inline so the API remains usable
    during development.
    """

    def __init__(self) -> None:
        self._jobs: Dict[str, IngestJobStatus] = {}

    def ingest_bytes(
        self,
        *,
        source_type: str,
        source_uri: str,
        display_name: str,
        payload: Optional[bytes],
        extra_metadata: Optional[Dict[str, object]] = None,
    ) -> IngestJobStatus:
        report: FlowReport = document_intake_flow(
            source_type=source_type,
            source_uri=source_uri,
            display_name=display_name,
            content=payload,
            extra_metadata=extra_metadata,
        )
        status = self._report_to_status(report)
        self._jobs[status.job_id] = status
        return status

    def fetch_status(self, job_id: str) -> Optional[IngestJobStatus]:
        """Return a cached ingest status if available."""

        return self._jobs.get(job_id)

    def _report_to_status(self, report: FlowReport) -> IngestJobStatus:
        ingest_item: IngestItem = report.ingest_item
        metadata: Dict[str, object] = dict(report.metadata)
        metadata.setdefault("embedding_spaces", report.embedding_spaces)
        metadata.setdefault("chunk_count", report.chunk_count)
        payload = IngestJobStatus(
            ingest_item_id=str(ingest_item.id),
            job_id=report.job_id or (str(ingest_item.job_id) if ingest_item.job_id else str(ingest_item.id)),
            status=ingest_item.status,
            source_type=ingest_item.source_type,
            display_name=ingest_item.display_name,
            domain=ingest_item.domain,
            domain_confidence=ingest_item.domain_confidence,
            chunk_count=report.chunk_count,
            embedding_spaces=report.embedding_spaces,
            metadata=metadata,
            document_summary=ingest_item.document_summary,
        )
        return payload


__all__ = ["IngestionService"]
