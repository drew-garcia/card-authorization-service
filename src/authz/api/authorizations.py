"""Routes for card authorizations."""

from typing import Annotated

from fastapi import APIRouter, Depends

from authz.api.deps import get_store
from authz.schemas import AuthorizationRequest, AuthorizationResponse
from authz.services.authorizations import authorize
from authz.store import CardStore

router = APIRouter(prefix="/authorizations", tags=["authorizations"])


@router.post("")
def create_authorization(
    request: AuthorizationRequest,
    store: Annotated[CardStore, Depends(get_store)],
) -> AuthorizationResponse:
    """Approve or decline a card swipe.

    Both outcomes are 200: a decline is a valid answer, not an error. An
    unknown card is a decline with reason card_not_found.
    """
    return authorize(store, request)
