"""make network_request_id unique and add request_hash

Revision ID: daa7aa521fce
Revises: cfe8e1763139
Create Date: 2026-11-24 22:28:34.506983

Adding a NOT NULL column with no default fails if authorizations has rows,
and the unique constraint fails if any network_request_id repeats. Both are
deliberate: old rows have no hash to backfill, so reset a dev database rather
than invent one.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "daa7aa521fce"
down_revision: str | Sequence[str] | None = "cfe8e1763139"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "authorizations",
        sa.Column("request_hash", sa.String(length=64), nullable=False),
    )
    # The unique constraint brings its own index, so the old one is redundant.
    op.drop_index(
        op.f("ix_authorizations_network_request_id"), table_name="authorizations"
    )
    op.create_unique_constraint(
        "uq_authorizations_network_request_id",
        "authorizations",
        ["network_request_id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        "uq_authorizations_network_request_id", "authorizations", type_="unique"
    )
    op.create_index(
        op.f("ix_authorizations_network_request_id"),
        "authorizations",
        ["network_request_id"],
        unique=False,
    )
    op.drop_column("authorizations", "request_hash")
