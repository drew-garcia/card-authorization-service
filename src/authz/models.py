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
    ForeignKey,
    Text,
    UniqueConstraint,
    Uuid,
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


# Every state a hold can be in. pending is the only one that can still change:
# the other four are final. models.py and schemas.py both list them; the unit
# tests check the two lists match.
HOLD_STATUSES = ("pending", "captured", "partially_captured", "reversed", "expired")


class HoldRow(SQLModel, table=True):
    """Money set aside by an approved authorization, until it is settled.

    The approval has already subtracted amount_cents from the card's
    available_cents. A capture keeps captured_cents of it and gives the rest
    back; a reversal gives all of it back.
    """

    __tablename__ = "holds"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_holds_amount_positive"),
        CheckConstraint(
            "status IN ('pending', 'captured', 'partially_captured', "
            "'reversed', 'expired')",
            name="ck_holds_status",
        ),
        # captured_cents must agree with status, so a buggy update can't leave
        # a "captured" hold with nothing captured, or a reversed one that kept
        # money.
        CheckConstraint(
            "(status IN ('pending', 'reversed', 'expired') AND captured_cents = 0)"
            " OR (status = 'captured' AND captured_cents = amount_cents)"
            " OR (status = 'partially_captured'"
            " AND captured_cents > 0 AND captured_cents < amount_cents)",
            name="ck_holds_captured_matches_status",
        ),
        # One hold per authorization. Captures and reversals find the hold by
        # authorization_id, and this gives them the index to do it.
        UniqueConstraint("authorization_id", name="uq_holds_authorization_id"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    # sa_column gives full control of the column, here to add a foreign key.
    authorization_id: UUID = Field(
        sa_column=Column(
            Uuid,
            ForeignKey("authorizations.id", name="fk_holds_authorization_id"),
            nullable=False,
        )
    )
    card_id: UUID = Field(
        sa_column=Column(
            Uuid, ForeignKey("cards.id", name="fk_holds_card_id"), nullable=False
        )
    )
    amount_cents: int = Field(sa_type=BigInteger)
    captured_cents: int = Field(default=0, sa_type=BigInteger)
    status: str = Field(default="pending", sa_type=Text)
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=func.now()
        ),
    )
    # Set to now() by the service on every status change.
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=func.now()
        ),
    )


class CaptureRow(SQLModel, table=True):
    """A successful capture of a hold. Rejected captures aren't stored."""

    __tablename__ = "captures"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_captures_amount_positive"),
        CheckConstraint("released_cents >= 0", name="ck_captures_released_nonnegative"),
        # The idempotency key (hard rule 4).
        UniqueConstraint("network_capture_id", name="uq_captures_network_capture_id"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    network_capture_id: str = Field(max_length=64)
    request_hash: str = Field(max_length=64)
    hold_id: UUID = Field(
        sa_column=Column(
            Uuid, ForeignKey("holds.id", name="fk_captures_hold_id"), nullable=False
        )
    )
    authorization_id: UUID
    amount_cents: int = Field(sa_type=BigInteger)
    # The part of the hold given back to the card: hold amount - amount_cents.
    released_cents: int = Field(sa_type=BigInteger)
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=func.now()
        ),
    )


class ReversalRow(SQLModel, table=True):
    """A successful reversal of a hold. Rejected reversals aren't stored."""

    __tablename__ = "reversals"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        CheckConstraint("released_cents > 0", name="ck_reversals_released_positive"),
        # The idempotency key (hard rule 4).
        UniqueConstraint(
            "network_reversal_id", name="uq_reversals_network_reversal_id"
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    network_reversal_id: str = Field(max_length=64)
    request_hash: str = Field(max_length=64)
    hold_id: UUID = Field(
        sa_column=Column(
            Uuid, ForeignKey("holds.id", name="fk_reversals_hold_id"), nullable=False
        )
    )
    authorization_id: UUID
    released_cents: int = Field(sa_type=BigInteger)
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=func.now()
        ),
    )
