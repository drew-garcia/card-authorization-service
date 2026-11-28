"""index pending holds by created_at

Revision ID: 230f6c3c0ce8
Revises: be888909e8cd
Create Date: 2026-11-28 02:13:53.907382

The hold expiry worker repeatedly asks for the oldest pending holds. This
partial index covers only pending holds, so that query stays fast however many
settled holds accumulate.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "230f6c3c0ce8"
down_revision: str | Sequence[str] | None = "be888909e8cd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        "ix_holds_pending_created_at",
        "holds",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_holds_pending_created_at",
        table_name="holds",
        postgresql_where=sa.text("status = 'pending'"),
    )
