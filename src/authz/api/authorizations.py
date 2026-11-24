"""Routes for card authorizations."""

from fastapi import APIRouter, HTTPException, status

from authz.api.deps import SessionDep
from authz.schemas import AuthorizationRequest, AuthorizationResponse
from authz.services.authorizations import IdempotencyConflict, authorize

router = APIRouter(prefix="/authorizations", tags=["authorizations"])

CONFLICT_DETAIL = "network_request_id was already used with a different request body"


@router.post(
    "",
    # Listed so /docs shows the 409; FastAPI only documents 200 and 422 itself.
    responses={status.HTTP_409_CONFLICT: {"description": CONFLICT_DETAIL}},
)
def create_authorization(
    request: AuthorizationRequest, session: SessionDep
) -> AuthorizationResponse:
    """Approve or decline a card swipe.

    Both outcomes are 200: a decline is a valid answer, not an error. An
    unknown card is a decline with reason card_not_found. A repeat of a
    network_request_id with the same body returns the original response; with
    a different body it is a 409.
    """
    try:
        return authorize(session, request)
    except IdempotencyConflict:
        # The service doesn't know about HTTP; the route maps its error.
        raise HTTPException(status.HTTP_409_CONFLICT, detail=CONFLICT_DETAIL) from None
