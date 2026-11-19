"""Database engine and per-request sessions."""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine
from sqlmodel import Session, create_engine

from authz.settings import get_settings


@lru_cache
def get_engine() -> Engine:
    """One engine (and connection pool) per process, built on first use.

    Built lazily rather than at import time, so importing the app doesn't need
    DATABASE_URL to be set (tests swap in their own engine).
    """
    return create_engine(get_settings().database_url, pool_pre_ping=True)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, closed when it ends.

    A generator dependency: FastAPI runs the code before `yield`, hands the
    session to the route, then runs the rest (closing it) after the response.
    """
    with Session(get_engine()) as session:
        yield session
