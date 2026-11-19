"""In-memory card storage.

A stand-in for Postgres. Callers only use these methods, never the dict, so a
database-backed store with the same methods can replace this class later
without touching the service or the routes.
"""

from uuid import UUID, uuid4

from authz.schemas import Card


class CardStore:
    """Cards held in a dict: card id -> available balance in cents."""

    def __init__(self) -> None:
        self._available_cents: dict[UUID, int] = {}

    def create(self, initial_balance_cents: int) -> Card:
        card_id = uuid4()
        self._available_cents[card_id] = initial_balance_cents
        return Card(id=card_id, available_cents=initial_balance_cents)

    def get(self, card_id: UUID) -> Card | None:
        available = self._available_cents.get(card_id)
        if available is None:
            return None
        return Card(id=card_id, available_cents=available)

    def set_available(self, card_id: UUID, available_cents: int) -> None:
        if card_id not in self._available_cents:
            raise KeyError(card_id)
        self._available_cents[card_id] = available_cents
