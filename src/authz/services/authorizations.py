"""Business logic for approving or declining card swipes."""

from uuid import uuid4

from authz.schemas import AuthorizationRequest, AuthorizationResponse, DeclineReason
from authz.store import CardStore


def authorize(store: CardStore, request: AuthorizationRequest) -> AuthorizationResponse:
    """Approve the swipe if it fits the card's available balance, then subtract it.

    Not idempotent yet: a repeated network_request_id is decided again
    (hard rule 4 comes with Postgres).
    """
    card = store.get(request.card_id)
    if card is None:
        return _response(request, decline_reason="card_not_found")

    # Deliberate read-check-write (the CLAUDE.md exception to hard rule 3).
    # FastAPI runs sync routes in a thread pool, so two concurrent swipes can
    # both read the same balance and both be approved. The race test is meant
    # to catch this; don't add a lock here unless asked.
    if request.amount_cents > card.available_cents:
        return _response(request, decline_reason="insufficient_funds")

    store.set_available(card.id, card.available_cents - request.amount_cents)
    return _response(request, decline_reason=None)


def _response(
    request: AuthorizationRequest, decline_reason: DeclineReason | None
) -> AuthorizationResponse:
    return AuthorizationResponse(
        authorization_id=uuid4(),
        network_request_id=request.network_request_id,
        card_id=request.card_id,
        amount_cents=request.amount_cents,
        decision="approved" if decline_reason is None else "declined",
        decline_reason=decline_reason,
    )
