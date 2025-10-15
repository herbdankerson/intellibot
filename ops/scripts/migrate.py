"""Apply the SQL schema to the configured database."""

from __future__ import annotations

from pathlib import Path
import sys

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.append(str(BASE_DIR))

from src.my_agentic_chatbot.storage.connection import get_engine, run_sql_file
SCHEMA_PATH = BASE_DIR / "src" / "my_agentic_chatbot" / "storage" / "models.sql"


def main() -> None:
    engine = get_engine()
    run_sql_file(str(SCHEMA_PATH), engine=engine)
    print(f"Applied schema from {SCHEMA_PATH}")


if __name__ == "__main__":
    main()
