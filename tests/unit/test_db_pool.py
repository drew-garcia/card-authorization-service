"""The engine's connection pool is fixed-size and configurable.

Creating an engine doesn't connect to the database, so no Postgres is needed.
"""

import threading
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.pool import QueuePool

from authz.db import dispose_engine, get_engine
from authz.settings import get_settings

DUMMY_URL = "postgresql+psycopg://user:pass@localhost:1/none"


@pytest.fixture(autouse=True)
def _fresh_engine(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Build the settings and engine from this test's environment.

    Both are built once and cached, so they're cleared before the test (to
    pick up the patched variables) and after it (so later tests don't get
    this dummy engine).
    """
    monkeypatch.setenv("DATABASE_URL", DUMMY_URL)
    get_settings.cache_clear()
    dispose_engine()
    yield
    dispose_engine()
    get_settings.cache_clear()


def _pool_limits() -> tuple[int, int]:
    """(pool size, overflow allowed) of the app's engine."""
    pool = get_engine().pool
    assert isinstance(pool, QueuePool)
    # SQLAlchemy has no public getter for the overflow limit.
    return pool.size(), pool._max_overflow  # pyright: ignore[reportPrivateUsage]


def test_pool_is_fixed_size_by_default() -> None:
    assert _pool_limits() == (10, 0)


def test_pool_limits_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "3")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "2")
    assert _pool_limits() == (3, 2)


def test_concurrent_first_calls_share_one_engine() -> None:
    """The first burst of requests in a worker must not each build an engine.

    Every request calls get_engine() from its own threadpool thread. If two
    threads both find no engine yet and both build one, each extra engine
    brings its own pool, and the worker opens far more connections than
    db_pool_size allows. A Barrier releases all the threads at the same
    moment, so they really do race for the first call.
    """
    threads = 50
    barrier = threading.Barrier(threads)
    engines: list[Engine] = []

    def first_call() -> None:
        barrier.wait()
        engines.append(get_engine())  # list.append is thread-safe

    workers = [threading.Thread(target=first_call) for _ in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()

    assert len({id(e) for e in engines}) == 1
