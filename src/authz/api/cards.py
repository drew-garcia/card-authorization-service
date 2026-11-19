"""Routes for cards."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from authz.api.deps import get_store
from authz.schemas import Card, CardCreate
from authz.store import CardStore

router = APIRouter(prefix="/cards", tags=["cards"])

StoreDep = Annotated[CardStore, Depends(get_store)]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_card(body: CardCreate, store: StoreDep) -> Card:
    """Open a card with a starting balance."""
    return store.create(body.initial_balance_cents)


@router.get("/{card_id}")
def get_card(card_id: UUID, store: StoreDep) -> Card:
    """Look up a card and its available balance."""
    card = store.get(card_id)
    if card is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Card not found."
        )
    return card
