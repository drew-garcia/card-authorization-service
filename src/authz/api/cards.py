"""Routes for cards."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from authz.api.deps import SessionDep
from authz.schemas import Card, CardCreate
from authz.services import cards

router = APIRouter(prefix="/cards", tags=["cards"])


@router.post("", status_code=status.HTTP_201_CREATED)
def create_card(body: CardCreate, session: SessionDep) -> Card:
    """Open a card with a starting balance."""
    return cards.create_card(session, body.initial_balance_cents)


@router.get("/{card_id}")
def get_card(card_id: UUID, session: SessionDep) -> Card:
    """Look up a card and its available balance."""
    card = cards.get_card(session, card_id)
    if card is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Card not found."
        )
    return card
