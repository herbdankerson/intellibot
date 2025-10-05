import hashlib
import os
import sys
import uuid
from typing import List

from src.config import (
    CHUNK_TOKENS,
    DATA_PARSED,
    OVERLAP_MAX_PCT,
    VOYAGE_MODEL_GENERAL,
    VOYAGE_MODEL_LEGAL,
    pg_dsn,
)
from src.chunker import build_chunks, rough_tokens
from src.db_write import (
    connect_db,
    fetch_doc_versions,
    insert_chunks,
    insert_doc_version,
    insert_tags,
)
from src.doc_parse import parse_pdf
from src.embed_voyage import embed_docs
from src.ner_tags import extract_tags


def enrich_chunk_embeddings(chunks: List[dict]) -> None:
    texts = [chunk["text"] for chunk in chunks]
    emb_general = embed_docs(texts, VOYAGE_MODEL_GENERAL)
    emb_legal = embed_docs(texts, VOYAGE_MODEL_LEGAL)
    for idx, chunk in enumerate(chunks):
        chunk["emb_general"] = emb_general[idx]
        chunk["emb_legal"] = emb_legal[idx]


def ingest(pdf_path: str, kind: str = "case_filing") -> None:
    slug, markdown, _ = parse_pdf(pdf_path, DATA_PARSED)
    chunk_defs = build_chunks(markdown, CHUNK_TOKENS, OVERLAP_MAX_PCT)

    doc_emb_general = embed_docs([markdown], VOYAGE_MODEL_GENERAL)[0]
    doc_emb_legal = embed_docs([markdown], VOYAGE_MODEL_LEGAL)[0]
    doc_tags = extract_tags(markdown)

    for chunk in chunk_defs:
        chunk["id"] = str(uuid.uuid4())
        chunk["tags"] = extract_tags(chunk["text"])

    enrich_chunk_embeddings(chunk_defs)

    dsn = pg_dsn()
    with connect_db(dsn) as connection:
        existing = fetch_doc_versions(connection, slug)
        existing_hashes = {
            version: hashlib.sha256((row_content or "").encode("utf-8")).hexdigest()
            for _, version, row_content in existing
        }
        incoming_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()

        for version, content_hash in existing_hashes.items():
            if content_hash == incoming_hash:
                raise RuntimeError(
                    f"Document '{slug}' matches existing version {version}; ingestion skipped"
                )

        next_version = (max(existing_hashes.keys()) + 1) if existing_hashes else 1

        doc_id = insert_doc_version(
            connection,
            slug=slug,
            version=next_version,
            source_file=os.path.basename(pdf_path),
            kind=kind,
            content_md=markdown,
            token_count=rough_tokens(markdown),
            emb_general=doc_emb_general,
            emb_legal=doc_emb_legal,
        )
        insert_tags(connection, "doc_tags", "doc_id", doc_id, doc_tags)
        for chunk in chunk_defs:
            chunk["version"] = next_version
        insert_chunks(connection, doc_id, next_version, chunk_defs)
        for chunk in chunk_defs:
            insert_tags(connection, "chunk_tags", "chunk_id", chunk["id"], chunk["tags"])


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m src.ingest_one <PDF_PATH> [kind]", file=sys.stderr)
        sys.exit(1)
    try:
        ingest(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "case_filing")
    except RuntimeError as exc:
        print(f"[INFO] {exc}")
