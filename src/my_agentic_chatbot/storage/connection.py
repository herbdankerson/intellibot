"""Centralised database connection helpers for ParadeDB/Postgres."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator

import psycopg
from psycopg import Connection
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_settings

_ENGINE_CACHE: Dict[str, Engine] = {}
_DEFAULT_SQLA_URL = "postgresql+psycopg://agent:agentpass@paradedb:5432/agentdb"


def resolve_database_url(preferred: str | None = None) -> str:
    """Return the connection string to use for ParadeDB/Postgres access."""

    if preferred:
        return preferred
    env_override = os.getenv("PARADE_DB_DSN") or os.getenv("DATABASE_URL")
    if env_override:
        return env_override
    return get_settings().database_url or _DEFAULT_SQLA_URL


def _coerce_sqlalchemy_url(database_url: str) -> str:
    """Ensure SQLAlchemy URLs explicitly request the ``psycopg`` driver."""

    try:
        parsed = make_url(database_url)
    except Exception:  # pragma: no cover - let SQLAlchemy deal with bad URLs later
        return database_url

    if parsed.get_backend_name() != "postgresql":
        return database_url

    driver = parsed.drivername or ""
    if "+" in driver:
        return parsed.render_as_string(hide_password=False)

    coerced = parsed.set(drivername="postgresql+psycopg")
    return coerced.render_as_string(hide_password=False)


def _coerce_psycopg_url(database_url: str) -> str:
    """Strip SQLAlchemy driver segments so psycopg can parse the DSN."""

    try:
        parsed = make_url(database_url)
    except Exception:  # pragma: no cover - accept raw DSNs unchanged
        return database_url

    if parsed.get_backend_name() != "postgresql":
        return database_url

    driver = parsed.drivername or ""
    if "+" not in driver:
        return parsed.render_as_string(hide_password=False)

    base_driver = driver.split("+", 1)[0]
    coerced = parsed.set(drivername=base_driver)
    return coerced.render_as_string(hide_password=False)


def get_engine(url: str | None = None) -> Engine:
    """Return a cached SQLAlchemy engine for the configured database."""

    original_url = resolve_database_url(url)
    target_url = _coerce_sqlalchemy_url(original_url)
    engine = _ENGINE_CACHE.get(target_url)
    if engine is None:
        engine = create_engine(target_url, future=True)
        _ENGINE_CACHE[target_url] = engine
        _ENGINE_CACHE[original_url] = engine
    return engine


@contextmanager
def psycopg_connection(url: str | None = None, **kwargs) -> Iterator[Connection]:
    """Yield a psycopg connection and close it afterwards."""

    dsn = _coerce_psycopg_url(resolve_database_url(url))
    connection = psycopg.connect(dsn, **kwargs)
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Provide a transactional scope around database operations."""

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
    """Execute the statements contained in ``path`` against the database."""

    sql_text = Path(path).read_text()
    target_engine = engine or get_engine()
    with target_engine.begin() as connection:
        connection.exec_driver_sql(sql_text)


__all__ = [
    "get_engine",
    "psycopg_connection",
    "resolve_database_url",
    "run_sql_file",
    "session_scope",
]
