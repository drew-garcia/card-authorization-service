"""Database tables.

These are SQLModel classes with table=True: each one is both a Pydantic model
and a SQLAlchemy table. They stay separate from the API models in schemas.py,
so the database layout can change without changing the HTTP contract.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Text,
    UniqueConstraint,
    func,
)
from sqlmodel import Field, SQLModel


class CardRow(SQLModel, table=True):
    __tablename__ = "cards"  # pyright: ignore[reportAssignmentType]
    # CHECK constraints are a last line of defence: even buggy code can't
    # store a negative balance. Explicit names make migrations stable.
    __table_args__ = (
        CheckConstraint("limit_cents >= 0", name="ck_cards_limit_nonnegative"),
        CheckConstraint("available_cents >= 0", name="ck_cards_available_nonnegative"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    # sa_type=BigInteger: SQLModel maps a plain int to 32-bit INTEGER, which
    # tops out around $21 million in cents. Money columns are BIGINT.
    limit_cents: int = Field(sa_type=BigInteger)
    available_cents: int = Field(sa_type=BigInteger)


class AuthorizationRow(SQLModel, table=True):
    __tablename__ = "authorizations"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_authorizations_amount_positive"),
        CheckConstraint(
            "decision IN ('approved', 'declined')", name="ck_authorizations_decision"
        ),
        # The idempotency key (hard rule 4). INSERT ... ON CONFLICT relies on
        # this; it also gives the index used to look a request up by its id.
        UniqueConstraint(
            "network_request_id", name="uq_authorizations_network_request_id"
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    # Unique: see uq_authorizations_network_request_id above.
    network_request_id: str = Field(max_length=64)
    # SHA-256 hex of the validated request body. A repeat with the same id but
    # a different hash is a different request reusing the id: 409.
    request_hash: str = Field(max_length=64)
    # Deliberately no foreign key: declines for unknown cards are recorded too,
    # and their card_id doesn't exist in cards.
    card_id: UUID
    amount_cents: int = Field(sa_type=BigInteger)
    decision: str = Field(sa_type=Text)
    decline_reason: str | None = Field(default=None, sa_type=Text)
    # server_default: Postgres fills this in with now() on insert.
    # timezone=True gives TIMESTAMPTZ, which stores an absolute moment.
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=func.now()
        ),
    )
