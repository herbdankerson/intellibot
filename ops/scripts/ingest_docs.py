"""Ingest local documents into the Postgres knowledge base."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Iterable, List

from sqlalchemy import text

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.append(str(BASE_DIR))

from src.my_agentic_chatbot.storage.db import get_engine
from src.my_agentic_chatbot.util.text import squeeze_whitespace


def iter_files(paths: Iterable[Path]) -> Iterable[Path]:
    for path in paths:
        if path.is_dir():
            for candidate in sorted(path.rglob("*.txt")):
                if candidate.is_file():
                    yield candidate
            for candidate in sorted(path.rglob("*.md")):
                if candidate.is_file():
                    yield candidate
        elif path.is_file():
            yield path


def chunk_text(text: str) -> List[str]:
    paragraphs = [para.strip() for para in text.split("\n\n") if para.strip()]
    if not paragraphs:
        return [text.strip()]
    chunks: List[str] = []
    current: List[str] = []
    token_threshold = 160
    for paragraph in paragraphs:
        tokens = paragraph.split()
        if len(current) + len(tokens) > token_threshold and current:
            chunks.append(" ".join(current))
            current = []
        current.extend(tokens)
    if current:
        chunks.append(" ".join(current))
    return chunks


def ingest_file(path: Path) -> None:
    raw_text = path.read_text(encoding="utf-8")
    normalized = squeeze_whitespace(raw_text)
    chunks = chunk_text(normalized)
    engine = get_engine()
    title = path.stem.replace("_", " ").title() or path.name
    external_id = str(path.resolve())
    with engine.begin() as connection:
        doc_result = connection.execute(
            text(
                """
                INSERT INTO kb_documents (external_id, title, uri, tsv)
                VALUES (:external_id, :title, :uri, to_tsvector('english', :body))
                ON CONFLICT (external_id) DO UPDATE
                SET title = EXCLUDED.title,
                    uri = EXCLUDED.uri,
                    tsv = EXCLUDED.tsv
                RETURNING id
                """
            ),
            {
                "external_id": external_id,
                "title": title,
                "uri": str(path.resolve()),
                "body": normalized,
            },
        )
        document_id = doc_result.scalar_one()
        connection.execute(
            text("DELETE FROM kb_chunks WHERE document_id = :doc_id"),
            {"doc_id": document_id},
        )
        for ordinal, chunk in enumerate(chunks, start=1):
            connection.execute(
                text(
                    """
                    INSERT INTO kb_chunks (document_id, ordinal, heading, content, tsv)
                    VALUES (:document_id, :ordinal, :heading, :content, to_tsvector('english', :content))
                    """
                ),
                {
                    "document_id": document_id,
                    "ordinal": ordinal,
                    "heading": None,
                    "content": chunk,
                },
            )
    snippet = normalized[:160].replace("\n", " ")
    print(f"Ingested {path.name} ({len(chunks)} chunks): {snippet}...")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest documents into the knowledge base")
    parser.add_argument("paths", nargs="+", type=Path, help="Files or directories to ingest")
    args = parser.parse_args()

    files = list(iter_files(args.paths))
    if not files:
        print("No documents found for ingestion.")
        return

    for file_path in files:
        ingest_file(file_path)


if __name__ == "__main__":
    main()
