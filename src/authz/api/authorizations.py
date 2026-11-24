"""Routes for card authorizations."""

from fastapi import APIRouter, Response

from authz.api.deps import SessionDep
from authz.schemas import AuthorizationRequest, AuthorizationResponse
from authz.services.authorizations import authorize

router = APIRouter(prefix="/authorizations", tags=["authorizations"])


@router.post("")
def create_authorization(
    request: AuthorizationRequest, session: SessionDep, response: Response
) -> AuthorizationResponse:
    """Approve or decline a card swipe.

    Both outcomes are 200: a decline is a valid answer, not an error. An
    unknown card is a decline with reason card_not_found.
    """
    result, retries = authorize(session, request)
    # BENCH ONLY: lets the timing script count way 3's retries.
    response.headers["X-Authz-Retries"] = str(retries)
    return result
