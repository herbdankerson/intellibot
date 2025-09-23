"""Placeholder document ingestion script."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.append(str(BASE_DIR))

from src.my_agentic_chatbot.util.text import squeeze_whitespace


def ingest(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    snippet = squeeze_whitespace(text)[:200]
    print(f"Ingested {path.name}: {snippet}...")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest documents into the knowledge base")
    parser.add_argument("paths", nargs="*", type=Path, help="Files to ingest")
    args = parser.parse_args()
    for target in args.paths:
        ingest(target)
    if not args.paths:
        print("No documents provided; nothing to ingest.")


if __name__ == "__main__":
    main()
