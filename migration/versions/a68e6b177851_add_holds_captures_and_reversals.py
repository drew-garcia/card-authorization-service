"""add holds, captures and reversals

Revision ID: a68e6b177851
Revises: daa7aa521fce
Create Date: 2026-11-26 04:40:19.372855

Approved authorizations made before this migration have already debited their
card but have no hold. The backfill gives each one a pending hold for its
amount, so they can be captured or reversed like new ones.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a68e6b177851"
down_revision: str | Sequence[str] | None = "daa7aa521fce"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "holds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("authorization_id", sa.Uuid(), nullable=False),
        sa.Column("card_id", sa.Uuid(), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("captured_cents", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status IN ('pending', 'reversed', 'expired') AND captured_cents = 0)"
            " OR (status = 'captured' AND captured_cents = amount_cents)"
            " OR (status = 'partially_captured'"
            " AND captured_cents > 0 AND captured_cents < amount_cents)",
            name="ck_holds_captured_matches_status",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'captured', 'partially_captured', "
            "'reversed', 'expired')",
            name="ck_holds_status",
        ),
        sa.CheckConstraint("amount_cents > 0", name="ck_holds_amount_positive"),
        sa.ForeignKeyConstraint(
            ["authorization_id"],
            ["authorizations.id"],
            name="fk_holds_authorization_id",
        ),
        sa.ForeignKeyConstraint(["card_id"], ["cards.id"], name="fk_holds_card_id"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("authorization_id", name="uq_holds_authorization_id"),
    )
    op.create_table(
        "captures",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("network_capture_id", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("hold_id", sa.Uuid(), nullable=False),
        sa.Column("authorization_id", sa.Uuid(), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("released_cents", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount_cents > 0", name="ck_captures_amount_positive"),
        sa.CheckConstraint(
            "released_cents >= 0", name="ck_captures_released_nonnegative"
        ),
        sa.ForeignKeyConstraint(["hold_id"], ["holds.id"], name="fk_captures_hold_id"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "network_capture_id", name="uq_captures_network_capture_id"
        ),
    )
    op.create_table(
        "reversals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("network_reversal_id", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("hold_id", sa.Uuid(), nullable=False),
        sa.Column("authorization_id", sa.Uuid(), nullable=False),
        sa.Column("released_cents", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("released_cents > 0", name="ck_reversals_released_positive"),
        sa.ForeignKeyConstraint(["hold_id"], ["holds.id"], name="fk_reversals_hold_id"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "network_reversal_id", name="uq_reversals_network_reversal_id"
        ),
    )
    # gen_random_uuid() is built into Postgres 13+. Declined authorizations
    # reserved nothing, so they get no hold.
    op.execute(
        """
        INSERT INTO holds (id, authorization_id, card_id, amount_cents,
                           captured_cents, status)
        SELECT gen_random_uuid(), id, card_id, amount_cents, 0, 'pending'
        FROM authorizations
        WHERE decision = 'approved'
        """
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops every hold, capture and reversal. Released cents stay released on
    the cards, so downgrading a database with real captures loses history.
    """
    op.drop_table("reversals")
    op.drop_table("captures")
    op.drop_table("holds")
