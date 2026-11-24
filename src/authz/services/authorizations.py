"""Business logic for approving or declining card swipes.

BENCH BRANCH ONLY: holds all three race fixes, picked by AUTHZ_RACE_STRATEGY,
so design-notes/race-fixes.md can compare them. Never merged.
"""

import random
import time

from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, col, select

from authz.models import AuthorizationRow, CardRow
from authz.schemas import (
    AuthorizationRequest,
    AuthorizationResponse,
    Decision,
    DeclineReason,
)
from authz.settings import get_settings

MAX_RETRIES = 3
SERIALIZATION_FAILURE = "40001"


class AuthorizationContendedError(Exception):
    """Way 3 gave up: every attempt hit a serialization failure."""


def _record(
    session: Session,
    request: AuthorizationRequest,
    decline_reason: DeclineReason | None,
) -> AuthorizationResponse:
    """Insert the decision row and commit the transaction."""
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


def authorize_locked(
    session: Session, request: AuthorizationRequest
) -> AuthorizationResponse:
    """Way 1: SELECT ... FOR UPDATE, check in Python, write back."""
    statement = select(CardRow).where(CardRow.id == request.card_id).with_for_update()
    card = session.exec(statement).first()
    if card is None:
        decline_reason: DeclineReason | None = "card_not_found"
    elif request.amount_cents > card.available_cents:
        decline_reason = "insufficient_funds"
    else:
        decline_reason = None
        card.available_cents -= request.amount_cents
    return _record(session, request, decline_reason)


def authorize_conditional(
    session: Session, request: AuthorizationRequest
) -> AuthorizationResponse:
    """Way 2: one conditional UPDATE ... RETURNING. No row back means decline."""
    statement = (
        update(CardRow)
        .where(
            col(CardRow.id) == request.card_id,
            col(CardRow.available_cents) >= request.amount_cents,
        )
        .values(available_cents=col(CardRow.available_cents) - request.amount_cents)
        .returning(col(CardRow.available_cents))
    )
    new_balance = session.exec(statement).scalar_one_or_none()
    if new_balance is not None:
        decline_reason: DeclineReason | None = None
    else:
        exists = session.exec(select(CardRow.id).where(CardRow.id == request.card_id))
        found = exists.first() is not None
        decline_reason = "insufficient_funds" if found else "card_not_found"
    return _record(session, request, decline_reason)


def authorize_serializable(
    session: Session, request: AuthorizationRequest
) -> tuple[AuthorizationResponse, int]:
    """Way 3: plain read-check-write in a SERIALIZABLE transaction, retried on 40001.

    Returns the response and how many retries it took.
    """
    for attempt in range(MAX_RETRIES + 1):
        session.connection(execution_options={"isolation_level": "SERIALIZABLE"})
        try:
            card = session.exec(
                select(CardRow).where(CardRow.id == request.card_id)
            ).first()
            if card is None:
                decline_reason: DeclineReason | None = "card_not_found"
            elif request.amount_cents > card.available_cents:
                decline_reason = "insufficient_funds"
            else:
                decline_reason = None
                card.available_cents -= request.amount_cents
            return _record(session, request, decline_reason), attempt
        except OperationalError as e:
            session.rollback()
            if getattr(e.orig, "sqlstate", None) != SERIALIZATION_FAILURE:
                raise
            if attempt < MAX_RETRIES:
                time.sleep(random.uniform(0, 0.005 * 2**attempt))
    raise AuthorizationContendedError


def authorize(
    session: Session, request: AuthorizationRequest
) -> tuple[AuthorizationResponse, int]:
    """Dispatch on AUTHZ_RACE_STRATEGY. Returns (response, retries)."""
    strategy = get_settings().authz_race_strategy
    if strategy == "lock":
        return authorize_locked(session, request), 0
    if strategy == "conditional":
        return authorize_conditional(session, request), 0
    return authorize_serializable(session, request)
