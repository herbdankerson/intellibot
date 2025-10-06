"""Data models used across the ingestion Prefect flow.

These dataclasses describe the artefacts passed between individual Prefect
tasks so that the flow remains type-safe and easy to reason about while the
actual persistence layer is still being implemented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4


@dataclass
class IngestItem:
    """Metadata describing a single ingestion artefact."""

    id: UUID
    job_id: Optional[UUID]
    source_type: str
    source_uri: str
    display_name: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    status: str = "registered"
    domain: Optional[str] = None
    domain_confidence: Optional[float] = None
    document_summary: Optional[str] = None
    chunk_summaries: Dict[int, str] = field(default_factory=dict)

    def with_domain(self, domain: str, confidence: Optional[float]) -> "IngestItem":
        clone = self.copy()
        clone.domain = domain
        clone.domain_confidence = confidence
        return clone

    def with_status(self, status: str) -> "IngestItem":
        clone = self.copy()
        clone.status = status
        return clone

    def with_document_summary(self, summary: str) -> "IngestItem":
        clone = self.copy()
        clone.document_summary = summary
        return clone

    def with_chunk_summary(self, index: int, summary: str) -> "IngestItem":
        clone = self.copy()
        clone.chunk_summaries[index] = summary
        return clone

    def copy(self) -> "IngestItem":
        return IngestItem(
            id=self.id,
            job_id=self.job_id,
            source_type=self.source_type,
            source_uri=self.source_uri,
            display_name=self.display_name,
            metadata=dict(self.metadata),
            status=self.status,
            domain=self.domain,
            domain_confidence=self.domain_confidence,
            document_summary=self.document_summary,
            chunk_summaries=dict(self.chunk_summaries),
        )


@dataclass
class AcquiredSource:
    """Raw content retrieved from disk, HTTP, or other sources."""

    ingest_item_id: UUID
    content: bytes
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalizedDocument:
    """Docling-normalised representation of the source artefact."""

    ingest_item_id: UUID
    markdown: str
    text: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Chunk:
    """Single chunk extracted from the normalised document."""

    id: UUID
    ingest_item_id: UUID
    document_id: Optional[UUID]
    chunk_index: int
    heading_path: List[str]
    kind: str
    text: str
    token_count: int
    overlap_tokens: int
    ner_entities: List[Dict[str, Any]] = field(default_factory=list)
    summary: Optional[str] = None


@dataclass
class ChunkEmbedding:
    """Embedding payload ready to be stored in pgvector."""

    chunk_id: UUID
    space: str
    model: str
    vector: List[float]


@dataclass
class FlowReport:
    """Aggregate information returned by the Prefect flow."""

    ingest_item: IngestItem
    chunk_count: int
    embedding_spaces: List[str]
    job_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


def new_ingest_item(
    source_type: str,
    source_uri: str,
    display_name: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> IngestItem:
    """Helper that creates a new ingest item with a random UUID."""

    return IngestItem(
        id=uuid4(),
        job_id=uuid4(),
        source_type=source_type,
        source_uri=source_uri,
        display_name=display_name,
        metadata=metadata or {},
    )
