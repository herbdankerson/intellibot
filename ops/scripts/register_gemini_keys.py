"""Sync Gemini key metadata from gapistash vault into Postgres.

The script never reads or stores raw key values; it only registers the
`key_label` (e.g. ``GEMINI_KEY__gemini-1.5-flash__01``) and ties it to the
target model plus rotation priority. LiteLLM uses the same label to fetch the
secret from the vault at runtime.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Iterable, Iterator, List

from sqlalchemy import text

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.my_agentic_chatbot.llm_calls.gemini_key_manager import GeminiKeyManager, VaultEntry
from src.my_agentic_chatbot.storage.db import get_engine


def sync_keys(keys: Iterable[VaultEntry], models: List[str]) -> None:
    """Upsert keys into the registry/state tables."""

    engine = get_engine()
    with engine.begin() as conn:
        for key in keys:
            for model in models:
                conn.execute(
                    text(
                        """
                        INSERT INTO llm_key_registry (provider, model, key_label, priority)
                        VALUES (:provider, :model, :label, :priority)
                        ON CONFLICT (provider, model, key_label)
                        DO UPDATE SET priority = EXCLUDED.priority
                        """
                    ),
                    {
                        "provider": "gemini",
                        "model": model,
                        "label": key.label,
                        "priority": key.priority,
                    },
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO llm_key_state (provider, model, key_label, exhausted_at, available_after, last_error)
                        VALUES (:provider, :model, :label, NULL, NULL, NULL)
                        ON CONFLICT (provider, model, key_label)
                        DO UPDATE SET exhausted_at = NULL,
                                          available_after = NULL,
                                          last_error = NULL
                        """
                    ),
                    {
                        "provider": "gemini",
                        "model": model,
                        "label": key.label,
                    },
                )


def main() -> None:
    parser = argparse.ArgumentParser(description="Register Gemini API keys with the key pool")
    parser.add_argument(
        "--file",
        default=os.environ.get("GEMINI_KEY_VAULT_PATH", "gapistash2"),
        type=Path,
        help="Path to the gapistash vault file containing Gemini keys",
    )
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        default=[],
        help="Gemini model identifier to associate with the keys (repeatable)",
    )
    args = parser.parse_args()

    if not args.file.exists():
        raise SystemExit(f"Vault file not found: {args.file}")

    entries = GeminiKeyManager.parse_vault(args.file)
    keys = list(entries)
    if not keys:
        raise SystemExit("No Gemini keys found in vault file")

    models = args.models or ["gemini/gemini-2.5-pro"]

    sync_keys(keys, models)
    print(
        f"Registered {len(keys)} Gemini keys across models {', '.join(models)}"
    )


if __name__ == "__main__":
    main()
