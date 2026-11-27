"""add ledger transactions and entries

Revision ID: be888909e8cd
Revises: a68e6b177851
Create Date: 2026-11-27 23:43:01.947280

Adds the double-entry ledger, plus a deferred constraint trigger that refuses
to commit a ledger transaction whose entries don't sum to zero.

Captures made before this migration already moved money but posted nothing.
The backfill gives each one the ledger transaction it would have posted, so
the ledger agrees with the holds from the start.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "be888909e8cd"
down_revision: str | Sequence[str] | None = "a68e6b177851"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "ledger_transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("capture_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('capture')", name="ck_ledger_transactions_kind"),
        sa.ForeignKeyConstraint(
            ["capture_id"], ["captures.id"], name="fk_ledger_transactions_capture_id"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("capture_id", name="uq_ledger_transactions_capture_id"),
    )
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ledger_transaction_id", sa.Uuid(), nullable=False),
        sa.Column("account", sa.Text(), nullable=False),
        sa.Column("card_id", sa.Uuid(), nullable=True),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(account = 'card_spend') = (card_id IS NOT NULL)",
            name="ck_ledger_entries_card_matches_account",
        ),
        sa.CheckConstraint(
            "account IN ('card_spend', 'merchant_settlement')",
            name="ck_ledger_entries_account",
        ),
        sa.CheckConstraint(
            "amount_cents <> 0", name="ck_ledger_entries_amount_nonzero"
        ),
        sa.ForeignKeyConstraint(
            ["card_id"], ["cards.id"], name="fk_ledger_entries_card_id"
        ),
        sa.ForeignKeyConstraint(
            ["ledger_transaction_id"],
            ["ledger_transactions.id"],
            name="fk_ledger_entries_ledger_transaction_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_ledger_entries_card_id"), "ledger_entries", ["card_id"], unique=False
    )
    op.create_index(
        op.f("ix_ledger_entries_ledger_transaction_id"),
        "ledger_entries",
        ["ledger_transaction_id"],
        unique=False,
    )

    # Hard rule 5, enforced by Postgres. A CHECK constraint sees one row, and
    # the two entries of a transaction are inserted one at a time, so after
    # the first one the sum is briefly not zero. A constraint trigger declared
    # DEFERRABLE INITIALLY DEFERRED runs at COMMIT instead of after each
    # statement, by which time every entry is in. It runs once per inserted,
    # updated or deleted row and re-sums that row's transaction; if any sum
    # isn't zero, the RAISE aborts the commit and nothing is saved.
    op.execute(
        """
        CREATE FUNCTION ledger_entries_check_balanced() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            txn uuid;
            total bigint;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                txn := OLD.ledger_transaction_id;
            ELSE
                txn := NEW.ledger_transaction_id;
            END IF;
            SELECT COALESCE(SUM(amount_cents), 0) INTO total
            FROM ledger_entries WHERE ledger_transaction_id = txn;
            IF total <> 0 THEN
                RAISE EXCEPTION
                    'ledger transaction % is unbalanced: entries sum to %',
                    txn, total
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_ledger_entries_balanced
        AFTER INSERT OR UPDATE OR DELETE ON ledger_entries
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION ledger_entries_check_balanced()
        """
    )

    # Backfill. A temporary table pairs each existing capture with a new
    # transaction id, so the entries can find their transaction. The trigger
    # above checks these too, when the migration commits.
    op.execute(
        """
        CREATE TEMPORARY TABLE capture_backfill AS
        SELECT gen_random_uuid() AS txn_id, c.id AS capture_id,
               c.amount_cents, c.created_at, h.card_id
        FROM captures c JOIN holds h ON h.id = c.hold_id
        """
    )
    op.execute(
        """
        INSERT INTO ledger_transactions (id, kind, capture_id, created_at)
        SELECT txn_id, 'capture', capture_id, created_at FROM capture_backfill
        """
    )
    op.execute(
        """
        INSERT INTO ledger_entries
            (id, ledger_transaction_id, account, card_id, amount_cents, created_at)
        SELECT gen_random_uuid(), txn_id, 'card_spend', card_id,
               -amount_cents, created_at
        FROM capture_backfill
        UNION ALL
        SELECT gen_random_uuid(), txn_id, 'merchant_settlement', NULL,
               amount_cents, created_at
        FROM capture_backfill
        """
    )
    op.execute("DROP TABLE capture_backfill")


def downgrade() -> None:
    """Downgrade schema.

    Drops the whole ledger. Captures and holds are kept, so upgrading again
    rebuilds the capture postings from them through the backfill.
    """
    op.execute("DROP TRIGGER trg_ledger_entries_balanced ON ledger_entries")
    op.execute("DROP FUNCTION ledger_entries_check_balanced()")
    op.drop_index(
        op.f("ix_ledger_entries_ledger_transaction_id"), table_name="ledger_entries"
    )
    op.drop_index(op.f("ix_ledger_entries_card_id"), table_name="ledger_entries")
    op.drop_table("ledger_entries")
    op.drop_table("ledger_transactions")
