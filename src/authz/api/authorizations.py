"""Routes for card authorizations."""

from fastapi import APIRouter, HTTPException, status

from authz.schemas import AuthorizationRequest

router = APIRouter(prefix="/authorizations", tags=["authorizations"])


@router.post("")
def create_authorization(request: AuthorizationRequest) -> None:
    """Approve or decline a card swipe.

    Stub: FastAPI validates the body against AuthorizationRequest (an invalid
    body gets a 422 before this function runs), then this returns 501 until
    the authorization service exists.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Authorization decisions are not implemented yet.",
    )
