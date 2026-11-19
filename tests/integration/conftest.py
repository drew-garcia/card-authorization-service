"""Fixtures for tests that run against a real Postgres database.

They use the authz_test database (created by docker/initdb), never the dev
one. Start it first with `docker compose up -d`.
"""

from collections.abc import Iterator

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, make_url, text
from sqlmodel import Session, create_engine

from alembic import command
from authz.db import get_session
from authz.main import app
from authz.settings import get_settings


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """An engine on a freshly migrated authz_test database, shared by all tests.

    The schema is wiped and rebuilt by running the real Alembic migrations, so
    the tests also prove the migrations produce a working schema.
    """
    url = make_url(get_settings().database_url).set(database="authz_test")
    url_str = url.render_as_string(hide_password=False)
    engine = create_engine(url_str)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url_str)
    command.upgrade(config, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A session for checking rows directly. Tables are emptied after each test.

    TRUNCATE rather than rolling back a transaction, because the services
    commit their own transactions.
    """
    with Session(engine) as session:
        yield session
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE cards, authorizations"))


@pytest.fixture
def client(engine: Engine, session: Session) -> Iterator[TestClient]:
    """A TestClient whose requests each get their own session on authz_test.

    Depends on `session` so the tables are truncated after every client test.
    """

    def test_session() -> Iterator[Session]:
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = test_session
    yield TestClient(app)
    app.dependency_overrides.clear()
