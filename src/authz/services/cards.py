"""Business logic for opening and looking up cards."""

from uuid import UUID

from sqlmodel import Session

from authz.models import CardRow
from authz.schemas import Card


def create_card(session: Session, initial_balance_cents: int) -> Card:
    """Open a card whose limit and available balance both start at the given amount."""
    row = CardRow(
        limit_cents=initial_balance_cents, available_cents=initial_balance_cents
    )
    session.add(row)
    session.commit()
    return _to_schema(row)


def get_card(session: Session, card_id: UUID) -> Card | None:
    row = session.get(CardRow, card_id)
    return None if row is None else _to_schema(row)


def _to_schema(row: CardRow) -> Card:
    # Converting here keeps database rows out of the routes and the API.
    return Card(id=row.id, available_cents=row.available_cents)
