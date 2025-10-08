"""Reset ParadeDB knowledge base schemas while preserving cfg tables."""

from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import text

BASE_DIR = Path(__file__).resolve().parents[2]
import sys

sys.path.append(str(BASE_DIR))

from src.my_agentic_chatbot.config import get_settings
from src.my_agentic_chatbot.storage.db import get_engine

MODELS_SQL = Path("src/my_agentic_chatbot/storage/models.sql")
INDEX_SQL = Path("ops/scripts/sql/create_embedding_indexes.sql")


def _iter_schema_statements() -> list[str]:
    sql_text = MODELS_SQL.read_text()
    statements = [segment.strip() for segment in sql_text.split(";") if segment.strip()]
    filtered: list[str] = []
    for statement in statements:
        # Skip cfg.* DDL and seed statements to preserve runtime configuration.
        if "cfg." in statement.lower():
            continue
        filtered.append(statement)
    return filtered


def reset_schemas(*, drop_only: bool = False) -> None:
    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS kb CASCADE"))
        connection.execute(text("DROP SCHEMA IF EXISTS agent CASCADE"))
        if drop_only:
            return
        statements = _iter_schema_statements()
        for statement in statements:
            connection.execute(text(statement))
        _run_index_sql(connection)


def _run_index_sql(connection) -> None:
    if not INDEX_SQL.exists():
        return
    connection.execute(text(INDEX_SQL.read_text()))


def create_indexes() -> None:
    engine = get_engine()
    with engine.begin() as connection:
        _run_index_sql(connection)


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset kb/agent schemas while preserving cfg")
    parser.add_argument("--drop-only", action="store_true", help="Drop schemas without recreating tables")
    parser.add_argument(
        "--indexes-only",
        action="store_true",
        help="Rebuild embedding indexes without dropping schemas",
    )
    args = parser.parse_args()
    settings = get_settings()
    if args.drop_only and args.indexes_only:
        parser.error("--drop-only and --indexes-only are mutually exclusive")
    if args.indexes_only:
        print(f"Rebuilding embedding indexes using {settings.database_url}")
        create_indexes()
        print("Rebuilt embedding indexes.")
        return

    print(f"Resetting knowledge base using {settings.database_url}")
    reset_schemas(drop_only=args.drop_only)
    if args.drop_only:
        print("Dropped kb and agent schemas.")
    else:
        print("Recreated kb and agent schemas and tables from models.sql.")


if __name__ == "__main__":
    main()
