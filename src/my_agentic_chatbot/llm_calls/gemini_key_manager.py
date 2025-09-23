"""Gemini API key rotation utilities."""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.my_agentic_chatbot.storage.db import get_engine


PROVIDER = "gemini"
DEFAULT_COOLDOWN = timedelta(hours=24)


@dataclass(frozen=True)
class VaultEntry:
    """Represents a key stored in the gapistash vault."""

    label: str
    secret: str
    priority: int


@dataclass(frozen=True)
class KeySelection:
    """Result of acquiring a key for a model."""

    model: str
    label: str
    secret: str


class NoAvailableKeyError(RuntimeError):
    """Raised when no key is currently available for the requested model."""


class GeminiKeyManager:
    """Coordinate Gemini API key usage across LiteLLM workers."""

    def __init__(
        self,
        *,
        vault_path: Optional[str | Path] = None,
        cooldown: timedelta = DEFAULT_COOLDOWN,
        engine: Optional[Engine] = None,
    ) -> None:
        self.vault_path = Path(
            vault_path or os.environ.get("GEMINI_KEY_VAULT_PATH", "gapistash2")
        ).resolve()
        if not self.vault_path.exists():
            raise FileNotFoundError(f"Gemini key vault not found: {self.vault_path}")

        self.cooldown = cooldown
        self.engine: Engine = engine or get_engine()
        self._entries: Dict[str, VaultEntry] = {
            entry.label: entry for entry in self.parse_vault(self.vault_path)
        }
        if not self._entries:
            raise ValueError(f"No Gemini keys discovered in {self.vault_path}")

        self._timers: Dict[tuple[str, str], threading.Timer] = {}
        self._resume_schedules()

    # ------------------------------------------------------------------
    # Vault parsing helpers
    # ------------------------------------------------------------------
    @staticmethod
    def parse_vault(path: Path) -> List[VaultEntry]:
        """Parse a gapistash vault file into ordered entries."""

        entries: List[VaultEntry] = []
        order = 0
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            secret: str
            label: str
            if "=" in stripped:
                name, value = stripped.split("=", maxsplit=1)
                label = name.strip()
                secret = value.strip()
            else:
                label = f"gemini_key_{order + 1:02d}"
                secret = stripped
            if not secret:
                continue
            order += 1
            entries.append(VaultEntry(label=label, secret=secret, priority=order))
        return entries

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def acquire_key(self, model: str) -> KeySelection:
        """Return the highest-priority available key for ``model``."""

        candidates = self._fetch_registry(model)
        now = datetime.now(timezone.utc)
        for key_label, available_after in candidates:
            entry = self._entries.get(key_label)
            if not entry:
                continue
            if available_after and available_after > now:
                continue
            if available_after and available_after <= now:
                # Cooldown has expired but state not yet cleared.
                self._clear_state(model, key_label)
            return KeySelection(model=model, label=entry.label, secret=entry.secret)
        raise NoAvailableKeyError(f"All Gemini keys for {model} are in cooldown")

    def mark_exhausted(
        self,
        model: str,
        key_label: str,
        *,
        reason: str = "",
        cooldown: Optional[timedelta] = None,
    ) -> None:
        """Mark ``key_label`` as exhausted for ``model`` and schedule release."""

        duration = cooldown or self.cooldown
        now = datetime.now(timezone.utc)
        available_after = now + duration
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO llm_key_state (provider, model, key_label, exhausted_at, available_after, last_error)
                    VALUES (:provider, :model, :label, :now, :available_after, :reason)
                    ON CONFLICT (provider, model, key_label)
                    DO UPDATE SET exhausted_at = :now,
                                      available_after = :available_after,
                                      last_error = :reason
                    """
                ),
                {
                    "provider": PROVIDER,
                    "model": model,
                    "label": key_label,
                    "available_after": available_after,
                    "now": now,
                    "reason": reason[:512],
                },
            )
        self._schedule_release(model, key_label, available_after)

    def release_key(self, model: str, key_label: str) -> None:
        """Clear cooldown state for ``key_label`` immediately."""

        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE llm_key_state
                       SET exhausted_at = NULL,
                           available_after = NULL,
                           last_error = NULL
                     WHERE provider = :provider
                       AND model = :model
                       AND key_label = :label
                    """
                ),
                {"provider": PROVIDER, "model": model, "label": key_label},
            )
        timer_key = (model, key_label)
        timer = self._timers.pop(timer_key, None)
        if timer:
            timer.cancel()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _fetch_registry(self, model: str) -> List[tuple[str, Optional[datetime]]]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT r.key_label, r.priority, s.available_after
                      FROM llm_key_registry r
                      LEFT JOIN llm_key_state s
                        ON r.provider = s.provider
                       AND r.model = s.model
                       AND r.key_label = s.key_label
                     WHERE r.provider = :provider
                       AND r.model = :model
                     ORDER BY r.priority ASC
                    """
                ),
                {"provider": PROVIDER, "model": model},
            ).fetchall()
        converted: List[tuple[str, Optional[datetime]]] = []
        for row in rows:
            available_after = row.available_after
            if isinstance(available_after, str):
                try:
                    available_after = datetime.fromisoformat(available_after)
                except ValueError:
                    available_after = None
            converted.append((row.key_label, available_after))
        return converted

    def _clear_state(self, model: str, key_label: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE llm_key_state
                       SET exhausted_at = NULL,
                           available_after = NULL,
                           last_error = NULL
                     WHERE provider = :provider
                       AND model = :model
                       AND key_label = :label
                    """
                ),
                {"provider": PROVIDER, "model": model, "label": key_label},
            )

    def _schedule_release(self, model: str, key_label: str, available_after: datetime) -> None:
        delay = (available_after - datetime.now(timezone.utc)).total_seconds()
        if delay <= 0:
            self.release_key(model, key_label)
            return
        timer_key = (model, key_label)
        timer = self._timers.pop(timer_key, None)
        if timer:
            timer.cancel()

        def _release() -> None:
            try:
                self.release_key(model, key_label)
            finally:
                self._timers.pop(timer_key, None)

        new_timer = threading.Timer(delay, _release)
        new_timer.daemon = True
        self._timers[timer_key] = new_timer
        new_timer.start()

    def _resume_schedules(self) -> None:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT model, key_label, available_after
                      FROM llm_key_state
                     WHERE provider = :provider
                       AND available_after IS NOT NULL
                    """
                ),
                {"provider": PROVIDER},
            ).fetchall()
        now = datetime.now(timezone.utc)
        for row in rows:
            label = row.key_label
            available_after: Optional[datetime] = row.available_after
            if not available_after:
                continue
            if available_after <= now:
                self.release_key(row.model, label)
            else:
                self._schedule_release(row.model, label, available_after)


__all__ = [
    "GeminiKeyManager",
    "KeySelection",
    "NoAvailableKeyError",
    "VaultEntry",
]
