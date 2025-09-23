"""Database connection helpers."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_settings

_ENGINE_CACHE: Dict[str, Engine] = {}


def get_engine(url: str | None = None) -> Engine:
    """Return a cached SQLAlchemy engine."""

    target_url = url or get_settings().database_url
    engine = _ENGINE_CACHE.get(target_url)
    if engine is None:
        engine = create_engine(target_url, future=True)
        _ENGINE_CACHE[target_url] = engine
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
