"""Logging configuration utilities."""

from __future__ import annotations

import logging
import logging.config
from typing import Any, Dict

from .config import get_settings

LOGGING_CONFIG: Dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        },
    },
    "handlers": {
        "default": {
            "level": "INFO",
            "class": "logging.StreamHandler",
            "formatter": "standard",
        }
    },
    "root": {
        "handlers": ["default"],
        "level": "INFO",
    },
}


def configure_logging() -> None:
    """Configure application logging if not already configured."""

    if not logging.getLogger().handlers:
        logging.config.dictConfig(LOGGING_CONFIG)
    settings = get_settings()
    logging.getLogger(__name__).info(
        "logging configured", extra={"component": "logging", "config": settings.to_metadata()}
    )


__all__ = ["configure_logging", "LOGGING_CONFIG"]
