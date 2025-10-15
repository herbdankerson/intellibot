"""Test configuration for path setup."""

from __future__ import annotations

import sys
from pathlib import Path

import os

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


_ENV_DEFAULTS = {
    "LITELLM_BASE_URL": "http://litellm:4000",
    "SEARCH_TOOLBOX_BASE_URL": "http://search-toolbox:8080",
    "DOCLING_BASE_URL": "http://docling:5001",
}


@pytest.fixture(autouse=True)
def _ensure_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate runtime environment defaults required for integration tests."""

    for key, default in _ENV_DEFAULTS.items():
        monkeypatch.setenv(key, os.getenv(key, default))
