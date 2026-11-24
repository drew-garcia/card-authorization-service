"""Business logic for approving or declining card swipes."""

from sqlalchemy import update
from sqlmodel import Session, col, select

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

    The check and the subtraction are one conditional UPDATE (hard rule 3):
    Postgres only subtracts if the balance still covers the amount, and
    returns the new balance. No row back means decline. A concurrent swipe on
    the same card waits on the row lock the UPDATE takes, then re-checks the
    WHERE clause against the committed balance. There is no read-then-write
    gap in Python, and no retries. design-notes/race-fixes.md compares this
    with SELECT ... FOR UPDATE and SERIALIZABLE.

    Not idempotent yet: a repeated network_request_id is decided again
    (hard rule 4).
    """
    # update() rather than text(): the column names are checked by pyright and
    # follow the model. col() tells pyright these are SQL columns, not ints.
    # The session begins a transaction on this first statement.
    statement = (
        update(CardRow)
        .where(
            col(CardRow.id) == request.card_id,
            col(CardRow.available_cents) >= request.amount_cents,
        )
        .values(available_cents=col(CardRow.available_cents) - request.amount_cents)
        .returning(col(CardRow.available_cents))
    )
    new_balance_cents = session.exec(statement).scalar_one_or_none()

    if new_balance_cents is not None:
        decline_reason: DeclineReason | None = None
    else:
        # Only a decline needs to know why. Reading here is safe: a decline
        # changes nothing, so a stale answer can't move money.
        card_exists = session.exec(
            select(CardRow.id).where(CardRow.id == request.card_id)
        ).first()
        decline_reason = "insufficient_funds" if card_exists else "card_not_found"

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
