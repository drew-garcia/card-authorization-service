"""Checks that the Alembic migrations and the SQLModel models agree."""

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect, text
from sqlmodel import SQLModel

import authz.models  # noqa: F401  # registers the tables on SQLModel.metadata


def test_models_match_migrations(engine: Engine) -> None:
    """The migrated schema has nothing autogenerate would change.

    Fails if a model changed without a new migration (the same check as
    `alembic check`).
    """
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), SQLModel.metadata)
    assert diff == []


def test_downgrade_then_upgrade(engine: Engine, alembic_config: Config) -> None:
    """Every downgrade() runs cleanly, and upgrading again rebuilds the tables."""
    try:
        command.downgrade(alembic_config, "base")
        assert inspect(engine).get_table_names() == ["alembic_version"]
    finally:
        # Always rebuild, so the tests after this one still have a schema.
        command.upgrade(alembic_config, "head")
    assert set(inspect(engine).get_table_names()) >= {"cards", "authorizations"}


def test_upgrade_backfills_holds_for_approved_authorizations(
    engine: Engine, alembic_config: Config
) -> None:
    """Approvals made before holds existed get a pending hold; declines don't."""
    try:
        command.downgrade(alembic_config, "daa7aa521fce")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO cards (id, limit_cents, available_cents)"
                    " VALUES (:card, 1000, 400)"
                ),
                {"card": CARD_ID},
            )
            conn.execute(
                text(
                    "INSERT INTO authorizations (id, network_request_id,"
                    " request_hash, card_id, amount_cents, decision, decline_reason)"
                    " VALUES (:a, 'old-ok', 'h1', :card, 600, 'approved', NULL),"
                    " (:d, 'old-no', 'h2', :card, 900, 'declined',"
                    " 'insufficient_funds')"
                ),
                {"a": APPROVED_ID, "d": DECLINED_ID, "card": CARD_ID},
            )
        command.upgrade(alembic_config, "head")

        with engine.connect() as conn:
            holds = conn.execute(
                text(
                    "SELECT authorization_id, card_id, amount_cents, captured_cents,"
                    " status FROM holds"
                )
            ).all()
        assert [tuple(map(str, h)) for h in holds] == [
            (APPROVED_ID, CARD_ID, "600", "0", "pending")
        ]
    finally:
        # Leave the schema at head and empty for the tests after this one.
        command.upgrade(alembic_config, "head")
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE cards, authorizations, holds CASCADE"))


CARD_ID = "11111111-1111-4111-8111-111111111111"
APPROVED_ID = "22222222-2222-4222-8222-222222222222"
DECLINED_ID = "33333333-3333-4333-8333-333333333333"
