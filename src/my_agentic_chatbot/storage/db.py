"""Database connection helpers."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_settings

_ENGINE_CACHE: Dict[str, Engine] = {}


def _coerce_postgres_driver(database_url: str) -> str:
    """Ensure Postgres URLs default to the psycopg (v3) driver.

    SQLAlchemy automatically prefers the legacy ``psycopg2`` driver when the URL
    does not specify a driver segment. Our runtime and docker images install the
    modern ``psycopg`` package instead, which raises ``ModuleNotFoundError`` at
    import time.  When that happens we rewrite the URL to explicitly target the
    ``postgresql+psycopg`` dialect and retry.

    The helper keeps non-Postgres URLs untouched so SQLite or alternative
    backends work exactly as before.
    """

    try:
        parsed = make_url(database_url)
    except Exception:  # pragma: no cover - let SQLAlchemy raise later
        return database_url

    if parsed.get_backend_name() != "postgresql":
        return database_url

    if parsed.drivername and "+" in parsed.drivername:
        return database_url

    return str(parsed.set(drivername="postgresql+psycopg"))


def get_engine(url: str | None = None) -> Engine:
    """Return a cached SQLAlchemy engine."""

    target_url = url or get_settings().database_url
    engine = _ENGINE_CACHE.get(target_url)
    if engine is None:
        try:
            engine = create_engine(target_url, future=True)
        except ModuleNotFoundError as exc:
            if exc.name == "psycopg2":
                coerced_url = _coerce_postgres_driver(target_url)
                logging.getLogger(__name__).info(
                    "Falling back to psycopg driver for database URL",
                    extra={"original_url": target_url, "coerced_url": coerced_url},
                )
                engine = create_engine(coerced_url, future=True)
                _ENGINE_CACHE[coerced_url] = engine
                _ENGINE_CACHE[target_url] = engine
            else:
                raise
        else:
            _ENGINE_CACHE[target_url] = engine
    return engine
    return engine


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Provide a transactional scope around a series of operations."""

    target_engine = engine or get_engine()
    factory = sessionmaker(bind=target_engine, future=True)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:  # pragma: no cover - defensive rollback
        session.rollback()
        raise
    finally:
        session.close()


def run_sql_file(path: str, engine: Engine | None = None) -> None:
    """Execute a SQL file against the target engine."""

    sql_text = Path(path).read_text()
    target_engine = engine or get_engine()
    with target_engine.begin() as connection:
        statements = [stmt.strip() for stmt in sql_text.split(";") if stmt.strip()]
        for statement in statements:
            connection.exec_driver_sql(statement)


__all__ = ["get_engine", "run_sql_file", "session_scope"]
