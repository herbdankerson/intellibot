"""Placeholder embedding job for development."""

from __future__ import annotations

import argparse


EMBEDDING_SPACES = ("emb-general", "emb-code", "emb-law")


def main() -> None:
    parser = argparse.ArgumentParser(description="Embed knowledge base chunks")
    parser.add_argument("--space", choices=EMBEDDING_SPACES, default="emb-general")
    args = parser.parse_args()
    print(f"Embedding chunks for space: {args.space}")


if __name__ == "__main__":
    main()
