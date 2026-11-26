"""Business logic for approving or declining card swipes."""

from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, col, select

from authz.models import AuthorizationRow, CardRow, HoldRow
from authz.schemas import (
    AuthorizationRequest,
    AuthorizationResponse,
    Decision,
    DeclineReason,
)

# Re-exported: they lived here before captures and reversals needed them too.
from authz.services.idempotency import IdempotencyConflict, request_hash

__all__ = ["IdempotencyConflict", "authorize", "request_hash"]


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

    Idempotent on network_request_id (hard rule 4). The record is written with
    INSERT ... ON CONFLICT DO NOTHING against a unique constraint. If another
    transaction holds the same id, Postgres makes this INSERT wait until that
    one commits, then inserts nothing. Nothing back means this is a repeat: we
    roll back, which undoes the debit above, and answer from the stored row.
    The same body hash gets the original response; a different one raises
    IdempotencyConflict (409). A repeat does take the card's row lock and
    debit briefly before rolling back; that was the trade-off chosen over
    claiming the id first, which would need a nullable decision column.

    An approval also places a pending hold for the amount, in the same
    transaction, so the debit and the hold always commit (or roll back)
    together. A repeat rolls back before the hold is written, so it never
    makes a second one.
    """
    hash_hex = request_hash(request)

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
    # A Core insert rather than session.add(): the ORM can't express
    # ON CONFLICT. id is passed explicitly because the model's default_factory
    # only runs when a model object is built, not for a Core insert.
    authorization_id = session.exec(
        insert(AuthorizationRow)
        .values(
            id=uuid4(),
            network_request_id=request.network_request_id,
            request_hash=hash_hex,
            card_id=request.card_id,
            amount_cents=request.amount_cents,
            decision=decision,
            decline_reason=decline_reason,
        )
        .on_conflict_do_nothing(index_elements=["network_request_id"])
        .returning(col(AuthorizationRow.id))
    ).scalar_one_or_none()

    if authorization_id is None:
        # A repeat. Roll back first so the debit above never commits, then
        # read the original. It is visible: ON CONFLICT only resolves once the
        # other transaction has committed, and under READ COMMITTED each new
        # statement sees everything committed before it starts.
        session.rollback()
        original = session.exec(
            select(AuthorizationRow).where(
                AuthorizationRow.network_request_id == request.network_request_id
            )
        ).one()
        if original.request_hash != hash_hex:
            raise IdempotencyConflict(request.network_request_id)
        return _to_response(original)

    if decision == "approved":
        session.add(
            HoldRow(
                authorization_id=authorization_id,
                card_id=request.card_id,
                amount_cents=request.amount_cents,
            )
        )
    # commit() flushes the pending HoldRow INSERT first.
    session.commit()
    return AuthorizationResponse(
        authorization_id=authorization_id,
        network_request_id=request.network_request_id,
        card_id=request.card_id,
        amount_cents=request.amount_cents,
        decision=decision,
        decline_reason=decline_reason,
    )


def _to_response(row: AuthorizationRow) -> AuthorizationResponse:
    """Rebuild the original response from its stored row.

    model_validate checks decision and decline_reason against the Literal
    types, since the row stores them as plain text.
    """
    return AuthorizationResponse.model_validate(
        {
            "authorization_id": row.id,
            "network_request_id": row.network_request_id,
            "card_id": row.card_id,
            "amount_cents": row.amount_cents,
            "decision": row.decision,
            "decline_reason": row.decline_reason,
        }
    )
