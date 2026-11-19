"""Business logic for approving or declining card swipes."""

from sqlmodel import Session

from authz.models import AuthorizationRow, CardRow
from authz.schemas import (
    AuthorizationRequest,
    AuthorizationResponse,
    Decision,
    DeclineReason,
)


def authorize(session: Session, request: AuthorizationRequest) -> AuthorizationResponse:
    """Approve the swipe if it fits the card's available balance, then subtract it.

    Every decision is recorded in the authorizations table. The balance change
    and that record are committed together, in one transaction.

    Not idempotent yet: a repeated network_request_id is decided again
    (hard rule 4).
    """
    card = session.get(CardRow, request.card_id)
    if card is None:
        decline_reason: DeclineReason | None = "card_not_found"
    elif request.amount_cents > card.available_cents:
        decline_reason = "insufficient_funds"
    else:
        decline_reason = None
        card.available_cents -= request.amount_cents

    decision: Decision = "approved" if decline_reason is None else "declined"
    row = AuthorizationRow(
        network_request_id=request.network_request_id,
        card_id=request.card_id,
        amount_cents=request.amount_cents,
        decision=decision,
        decline_reason=decline_reason,
    )
    session.add(row)
    session.commit()

    return AuthorizationResponse(
        authorization_id=row.id,
        network_request_id=request.network_request_id,
        card_id=request.card_id,
        amount_cents=request.amount_cents,
        decision=decision,
        decline_reason=decline_reason,
    )
