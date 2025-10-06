"""Prefect flow that orchestrates the document ingestion pipeline skeleton."""

from __future__ import annotations

from typing import Dict, Optional

from prefect import flow

from etl.tasks.intake_models import FlowReport, IngestItem
from etl.tasks import intake_tasks


@flow(name="document_intake_flow")
def document_intake_flow(
    *,
    source_type: str,
    source_uri: str,
    display_name: str,
    content: Optional[bytes] = None,
    extra_metadata: Optional[Dict[str, object]] = None,
) -> FlowReport:
    """Run the ingestion pipeline for the provided artefact."""

    item: IngestItem = intake_tasks.register_ingest_item(
        source_type=source_type,
        source_uri=source_uri,
        display_name=display_name,
        metadata=extra_metadata,
    )
    acquired = intake_tasks.acquire_source(item, content)
    normalized = intake_tasks.docling_normalize(acquired)

    item = intake_tasks.classify_domain_task(item, normalized)
    item = intake_tasks.summarize_document(item, normalized)

    chunks = intake_tasks.chunk_and_ner(item, normalized)
    item = intake_tasks.summarize_chunks(item, chunks)
    abstractions = intake_tasks.build_budgeted_abstractions(chunks)
    embeddings = intake_tasks.embed_chunks(item, chunks)

    report = intake_tasks.persist_results(item, normalized, chunks, embeddings, abstractions)

    if item.source_type == "document":
        intake_tasks.mirror_openwebui(item)

    item = intake_tasks.finalize_status(item, status="succeeded")
    report.ingest_item = item
    return report
