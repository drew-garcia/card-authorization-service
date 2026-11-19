"""create cards and authorizations tables

Revision ID: cfe8e1763139
Revises:
Create Date: 2026-11-19 20:05:53.346369

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cfe8e1763139"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "cards",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("limit_cents", sa.BigInteger(), nullable=False),
        sa.Column("available_cents", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("limit_cents >= 0", name="ck_cards_limit_nonnegative"),
        sa.CheckConstraint(
            "available_cents >= 0", name="ck_cards_available_nonnegative"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "authorizations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("network_request_id", sa.String(length=64), nullable=False),
        sa.Column("card_id", sa.Uuid(), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("decline_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "amount_cents > 0", name="ck_authorizations_amount_positive"
        ),
        sa.CheckConstraint(
            "decision IN ('approved', 'declined')", name="ck_authorizations_decision"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_authorizations_network_request_id"),
        "authorizations",
        ["network_request_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_authorizations_network_request_id"), table_name="authorizations"
    )
    op.drop_table("authorizations")
    op.drop_table("cards")
