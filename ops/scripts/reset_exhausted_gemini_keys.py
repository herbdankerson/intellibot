"""Clear Gemini key cooldowns once their timers expire."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

from sqlalchemy import text

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.my_agentic_chatbot.storage.db import get_engine


def reset_gemini_keys() -> int:
    """Reset exhausted keys whose cooldown windows have elapsed.

    Returns the number of key/model pairs that were re-enabled.
    """

    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                UPDATE llm_key_state
                   SET exhausted_at = NULL,
                       available_after = NULL,
                       last_error = NULL
                 WHERE provider = 'gemini'
                   AND available_after IS NOT NULL
                   AND available_after <= :now
                RETURNING provider
                """
            ),
            {"now": datetime.now(timezone.utc)},
        )
        rows = result.fetchall()
    return len(rows)


def main() -> None:
    cleared = reset_gemini_keys()
    print(f"Cleared cooldown for {cleared} Gemini key(s)")


if __name__ == "__main__":
    main()
