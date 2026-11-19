"""Routes for card authorizations."""

from fastapi import APIRouter

from authz.api.deps import SessionDep
from authz.schemas import AuthorizationRequest, AuthorizationResponse
from authz.services.authorizations import authorize

router = APIRouter(prefix="/authorizations", tags=["authorizations"])


@router.post("")
def create_authorization(
    request: AuthorizationRequest, session: SessionDep
) -> AuthorizationResponse:
    """Approve or decline a card swipe.

    Both outcomes are 200: a decline is a valid answer, not an error. An
    unknown card is a decline with reason card_not_found.
    """
    return authorize(session, request)
