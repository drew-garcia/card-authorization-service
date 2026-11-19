"""Checks that the Alembic migrations and the SQLModel models agree."""

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect
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
