"""Prefect flow that orchestrates the document ingestion pipeline skeleton."""

from __future__ import annotations

from typing import Dict, Optional

try:
    from prefect import flow
except ModuleNotFoundError:  # pragma: no cover - prefect optional for CLI usage
    def flow(function=None, *args, **kwargs):  # type: ignore
        if function is None:
            def decorator(fn):
                return fn

            return decorator
        return function

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
    target_namespace: str = "kb",
    target_entries: Optional[str] = None,
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
    chunk_emotions = intake_tasks.detect_emotions(item, chunks)
    abstractions = intake_tasks.build_budgeted_abstractions(chunks)
    embeddings = intake_tasks.embed_chunks(item, chunks)

    table_config = intake_tasks.table_config_from_namespace(
        target_namespace,
        entries=target_entries,
    )

    report = intake_tasks.persist_results(
        item,
        normalized,
        chunks,
        embeddings,
        chunk_emotions,
        abstractions,
        table_config=table_config,
    )

    if item.source_type == "document":
        intake_tasks.mirror_openwebui(item)

    item = intake_tasks.finalize_status(item, status="succeeded")
    report.ingest_item = item
    return report
