"""Routes for settling holds: captures and reversals."""

from typing import Any

from fastapi import APIRouter, HTTPException, status

from authz.api.deps import SessionDep
from authz.schemas import (
    CaptureRequest,
    CaptureResponse,
    ReversalRequest,
    ReversalResponse,
)
from authz.services import holds
from authz.services.idempotency import IdempotencyConflict

router = APIRouter(tags=["holds"])

NOT_FOUND_DETAIL = "authorization not found"
NOT_APPROVED_DETAIL = "authorization was declined, so it has no hold"
CAPTURE_CONFLICT_DETAIL = (
    "network_capture_id was already used with a different request body"
)
REVERSAL_CONFLICT_DETAIL = (
    "network_reversal_id was already used with a different request body"
)

# Listed so /docs shows them; FastAPI only documents 200 and 422 itself.
# Annotated because dict is invariant: pyright would infer dict[int, ...] and
# reject it where FastAPI expects dict[int | str, ...].
_ERRORS: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {"description": NOT_FOUND_DETAIL},
    status.HTTP_409_CONFLICT: {
        "description": "the hold can't make this move, or the network ID was "
        "reused with a different body"
    },
}


@router.post("/captures", responses=_ERRORS)
def create_capture(request: CaptureRequest, session: SessionDep) -> CaptureResponse:
    """Capture all or part of an approved authorization's hold.

    The captured amount stays spent; the rest of the hold goes back to the
    card. Only a pending hold can be captured, once, for at most its amount.
    """
    try:
        return holds.capture(session, request)
    except IdempotencyConflict:
        raise HTTPException(status.HTTP_409_CONFLICT, CAPTURE_CONFLICT_DETAIL) from None
    except (holds.InvalidHoldTransition, holds.CaptureExceedsHold) as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from None
    except holds.AuthorizationNotApproved:
        raise HTTPException(status.HTTP_409_CONFLICT, NOT_APPROVED_DETAIL) from None
    except holds.AuthorizationNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND_DETAIL) from None


@router.post("/reversals", responses=_ERRORS)
def create_reversal(request: ReversalRequest, session: SessionDep) -> ReversalResponse:
    """Release the whole hold of an approved authorization.

    Only a pending hold can be reversed.
    """
    try:
        return holds.reverse(session, request)
    except IdempotencyConflict:
        raise HTTPException(
            status.HTTP_409_CONFLICT, REVERSAL_CONFLICT_DETAIL
        ) from None
    except holds.InvalidHoldTransition as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from None
    except holds.AuthorizationNotApproved:
        raise HTTPException(status.HTTP_409_CONFLICT, NOT_APPROVED_DETAIL) from None
    except holds.AuthorizationNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND_DETAIL) from None
