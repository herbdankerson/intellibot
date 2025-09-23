"""Tests for the Gemini key manager failover logic."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from src.my_agentic_chatbot.llm_calls.gemini_key_manager import (
    GeminiKeyManager,
    NoAvailableKeyError,
)


def make_vault(tmp_path: Path, count: int = 3) -> Path:
    secrets = [f"fake-key-{idx}" for idx in range(count)]
    path = tmp_path / "gapistash2"
    path.write_text("\n".join(secrets), encoding="utf-8")
    return path


def make_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE llm_key_registry (
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                key_label TEXT NOT NULL,
                priority INTEGER NOT NULL,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (provider, model, key_label)
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE llm_key_state (
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                key_label TEXT NOT NULL,
                exhausted_at TIMESTAMPTZ,
                available_after TIMESTAMPTZ,
                last_error TEXT,
                PRIMARY KEY (provider, model, key_label)
            )
            """
        )
        for idx in range(3):
            conn.execute(
                text(
                    """
                    INSERT INTO llm_key_registry (provider, model, key_label, priority)
                    VALUES ('gemini', 'gemini/gemini-2.5-pro', :label, :priority)
                    """
                ),
                {
                    "label": f"gemini_key_{idx + 1:02d}",
                    "priority": idx + 1,
                },
            )
    return engine


def test_acquire_key_prefers_highest_priority(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    engine = make_engine()
    manager = GeminiKeyManager(vault_path=vault, engine=engine)
    selection = manager.acquire_key("gemini/gemini-2.5-pro")
    assert selection.label == "gemini_key_01"


def test_mark_exhausted_blocks_until_cooldown(tmp_path: Path) -> None:
    vault = make_vault(tmp_path, count=1)
    engine = make_engine()
    manager = GeminiKeyManager(vault_path=vault, engine=engine, cooldown=timedelta(seconds=1))
    selection = manager.acquire_key("gemini/gemini-2.5-pro")
    manager.mark_exhausted(selection.model, selection.label, reason="quota")
    with pytest.raises(NoAvailableKeyError):
        manager.acquire_key("gemini/gemini-2.5-pro")

    # Wait for the timer to run
    manager.release_key(selection.model, selection.label)
    reloaded = manager.acquire_key("gemini/gemini-2.5-pro")
    assert reloaded.label == selection.label
