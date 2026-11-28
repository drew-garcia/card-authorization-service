"""Database engine and per-request sessions."""

import threading
from collections.abc import Iterator

from sqlalchemy import Engine
from sqlmodel import Session, create_engine

from authz.settings import get_settings

_engine: Engine | None = None
_engine_lock = threading.Lock()


def get_engine() -> Engine:
    """One engine (and connection pool) per process, built on first use.

    Built lazily rather than at import time, so importing the app doesn't need
    DATABASE_URL to be set (tests swap in their own engine).

    Sync routes run in a threadpool, so a worker's first burst of requests
    calls this from many threads at once. functools.lru_cache, used here
    before, doesn't lock while it builds the value: every thread that found
    the cache empty built its own engine, each with its own pool, and one
    worker could open 40+ connections. The lock makes exactly one thread
    build the engine while the others wait for it. The check before the lock
    keeps the common case (engine already built) lock-free; the check inside
    it stops a thread that waited from building a second one.
    """
    global _engine  # assigning a module-level name inside a function needs this
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                settings = get_settings()
                # pool_size and max_overflow: see the comment on them in
                # settings.py. A request waiting for a connection holds no row
                # locks yet, so waiting can't deadlock; it gives up after
                # pool_timeout (30 s by default).
                _engine = create_engine(
                    settings.database_url,
                    pool_pre_ping=True,
                    pool_size=settings.db_pool_size,
                    max_overflow=settings.db_max_overflow,
                )
    return _engine


def dispose_engine() -> None:
    """Close the engine's connections and forget it; the next call builds a new one."""
    global _engine
    with _engine_lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, closed when it ends.

    A generator dependency: FastAPI runs the code before `yield`, hands the
    session to the route, then runs the rest (closing it) after the response.
    """
    with Session(get_engine()) as session:
        yield session
