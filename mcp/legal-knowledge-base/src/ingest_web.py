import hashlib
import sys
import uuid
from typing import Any, Dict, List

from src.chunker import build_chunks, rough_tokens
from src.config import (
    CHUNK_TOKENS,
    OVERLAP_MAX_PCT,
    VOYAGE_MODEL_GENERAL,
    VOYAGE_MODEL_LEGAL,
    pg_dsn,
)
from src.db_write import (
    connect_db,
    fetch_web_versions,
    insert_tags,
    insert_web_chunks,
    insert_web_version,
)
from src.embed_voyage import embed_docs
from src.ner_tags import extract_tags
from src.web_scrape import scrape_markdown, slug_from_url


def _embed_chunks(chunks: List[dict]) -> None:
    texts = [chunk["text"] for chunk in chunks]
    if not texts:
        return
    embeddings_general = embed_docs(texts, VOYAGE_MODEL_GENERAL)
    embeddings_legal = embed_docs(texts, VOYAGE_MODEL_LEGAL)
    for idx, chunk in enumerate(chunks):
        chunk["emb_general"] = embeddings_general[idx]
        chunk["emb_legal"] = embeddings_legal[idx]


def ingest_url(url: str) -> Dict[str, Any]:
    final_url, title, markdown = scrape_markdown(url)
    if not markdown.strip():
        raise RuntimeError(f"Scraped markdown from {final_url} is empty")

    slug = slug_from_url(final_url)
    chunks = build_chunks(markdown, CHUNK_TOKENS, OVERLAP_MAX_PCT)

    doc_embeddings_general = embed_docs([markdown], VOYAGE_MODEL_GENERAL)[0]
    doc_embeddings_legal = embed_docs([markdown], VOYAGE_MODEL_LEGAL)[0]

    doc_tags = extract_tags(markdown)
    for chunk in chunks:
        chunk["id"] = str(uuid.uuid4())
        chunk["tags"] = extract_tags(chunk["text"])

    _embed_chunks(chunks)

    dsn = pg_dsn()
    with connect_db(dsn) as connection:
        existing = fetch_web_versions(connection, slug)
        existing_hashes = {
            version: hashlib.sha256((content or "").encode("utf-8")).hexdigest()
            for _, version, content in existing
        }
        incoming_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        for version, content_hash in existing_hashes.items():
            if content_hash == incoming_hash:
                raise RuntimeError(
                    f"Web document '{slug}' matches existing version {version}; ingestion skipped"
                )

        next_version = (max(existing_hashes.keys()) + 1) if existing_hashes else 1
        web_id = insert_web_version(
            connection,
            url=final_url,
            slug=slug,
            version=next_version,
            title=title,
            content_md=markdown,
            token_count=rough_tokens(markdown),
            emb_general=doc_embeddings_general,
            emb_legal=doc_embeddings_legal,
        )
        insert_tags(connection, "web_tags", "web_id", web_id, doc_tags)

        for chunk in chunks:
            chunk["version"] = next_version
        insert_web_chunks(connection, web_id, next_version, chunks)
        for chunk in chunks:
            insert_tags(connection, "web_chunk_tags", "chunk_id", chunk["id"], chunk["tags"])

    return {
        "url": final_url,
        "slug": slug,
        "title": title,
        "version": next_version,
        "web_id": web_id,
        "chunk_count": len(chunks),
        "tag_counts": {
            "document": len(doc_tags),
            "chunks": sum(len(chunk["tags"]) for chunk in chunks),
        },
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m src.ingest_web <URL>", file=sys.stderr)
        raise SystemExit(1)
    try:
        result = ingest_url(sys.argv[1])
        print(
            "Ingested {slug} v{version} ({chunk_count} chunks)".format(
                slug=result["slug"],
                version=result["version"],
                chunk_count=result["chunk_count"],
            )
        )
    except RuntimeError as exc:
        print(f"[INFO] {exc}")
