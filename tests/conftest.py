"""Database fixtures shared by every test folder that talks to Postgres.

They use the authz_test database (created by docker/initdb), never the dev
one. Start it first with `docker compose up -d`.

Nothing here is autouse: a fixture in this root conftest only runs when a test
(or another fixture) asks for it, so unit tests don't need a database. The
integration/ and concurrency/ conftests opt in to clean_tables for their tests.
"""

from collections.abc import Callable, Iterator
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, make_url, text
from sqlmodel import Session, SQLModel, create_engine

import authz.models  # noqa: F401  # registers the tables on SQLModel.metadata
from authz.schemas import AuthorizationRequest
from authz.services.authorizations import authorize
from authz.services.cards import create_card
from authz.settings import get_settings

# Creates a card with the given balance in cents and returns its id.
MakeCard = Callable[[int], str]
# Places an approved hold: (card_id, amount_cents, age_seconds) -> authorization id.
MakeHold = Callable[[str, int, int], str]


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """The configured DATABASE_URL, pointed at authz_test instead of the dev db."""
    url = make_url(get_settings().database_url).set(database="authz_test")
    return url.render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def alembic_config(test_database_url: str) -> Config:
    """Alembic config aimed at authz_test. env.py keeps a URL that's already set."""
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", test_database_url)
    return config


@pytest.fixture(scope="session")
def engine(test_database_url: str, alembic_config: Config) -> Iterator[Engine]:
    """An engine on a freshly migrated authz_test database, shared by all tests.

    The schema is wiped and rebuilt by running the real Alembic migrations, once
    per test run, so the tests also prove the migrations produce a working schema.
    """
    engine = create_engine(test_database_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    command.upgrade(alembic_config, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def clean_tables(engine: Engine) -> None:
    """Empty every table, so no test sees another's rows.

    It runs before the test rather than after, so a test that crashed (or an
    aborted earlier run) can't leak rows into the next one. TRUNCATE rather
    than a rolled-back transaction, because the services commit their own
    transactions. The table list comes from the models, so new tables are
    covered automatically; alembic_version isn't a model, so it's kept.
    """
    tables = ", ".join(t.name for t in SQLModel.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A session for checking rows directly with SQL."""
    with Session(engine) as session:
        yield session


@pytest.fixture
def make_card(engine: Engine) -> MakeCard:
    """Returns a function that creates a card and gives back its id.

    It calls the card service directly, so tests that only need a card don't
    depend on POST /cards working. Each call uses its own short session, like
    a request would.
    """

    def _make_card(balance_cents: int) -> str:
        with Session(engine) as s:
            return str(create_card(s, balance_cents).id)

    return _make_card


@pytest.fixture
def make_hold(engine: Engine) -> MakeHold:
    """Returns a function that places a pending hold and makes it look old.

    The hold comes from the real authorize() service, so the card is debited
    exactly as a swipe would debit it. Then created_at is moved age_seconds
    into the past, which is how the expiry tests get holds old enough to
    expire without waiting.
    """
    counter = 0

    def _make_hold(card_id: str, amount_cents: int, age_seconds: int) -> str:
        nonlocal counter  # rebinding a variable of the enclosing function
        counter += 1
        request = AuthorizationRequest(
            network_request_id=f"hold-{counter}",
            card_id=UUID(card_id),
            amount_cents=amount_cents,
            currency="USD",
            merchant_name="Corner Coffee",
            merchant_category_code="5814",
        )
        with Session(engine) as s:
            response = authorize(s, request)
        assert response.decision == "approved"
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE holds SET created_at = now() - make_interval(secs => :age)"
                    " WHERE authorization_id = :a"
                ),
                {"age": age_seconds, "a": response.authorization_id},
            )
        return str(response.authorization_id)

    return _make_hold
