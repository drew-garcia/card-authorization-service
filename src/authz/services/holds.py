"""Business logic for settling holds: captures and reversals.

An approved authorization places a pending hold (see authorize()). From
pending, a hold moves exactly once:

    pending -> captured            capture of the whole hold
    pending -> partially_captured  capture of part of it; the rest is released
    pending -> reversed            reversal; all of it is released
    pending -> expired             too old; all of it is released (expire_holds)

The other four states are final. Any other move raises InvalidHoldTransition,
which the API turns into a 409.

"Released" means added back to the card's available_cents, which the approval
already subtracted the whole hold from.
"""

from collections import defaultdict
from datetime import timedelta
from typing import NoReturn
from uuid import UUID, uuid4

from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, col, select

from authz.models import AuthorizationRow, CaptureRow, CardRow, HoldRow, ReversalRow
from authz.schemas import (
    CaptureRequest,
    CaptureResponse,
    HoldStatus,
    ReversalRequest,
    ReversalResponse,
)
from authz.services import ledger
from authz.services.idempotency import IdempotencyConflict, request_hash


class AuthorizationNotFound(Exception):
    """No authorization has this id."""


class AuthorizationNotApproved(Exception):
    """The authorization was declined, so it has no hold to settle."""


class InvalidHoldTransition(Exception):
    """The hold's current state doesn't allow this move."""

    def __init__(self, action: str, status: str) -> None:
        super().__init__(f"cannot {action} a hold that is {status}")
        self.action = action
        self.status = status


class CaptureExceedsHold(Exception):
    """A capture asked for more than the hold sets aside."""

    def __init__(self, capture_cents: int, hold_cents: int) -> None:
        super().__init__(
            f"capture of {capture_cents} cents exceeds the hold of {hold_cents} cents"
        )


# The state rules. Pure functions with no database, so the unit tests can
# check every state against every action directly.


def capture_outcome(status: str, hold_cents: int, capture_cents: int) -> HoldStatus:
    """The state a capture moves the hold to, or an error if it can't."""
    if status != "pending":
        raise InvalidHoldTransition("capture", status)
    if capture_cents > hold_cents:
        raise CaptureExceedsHold(capture_cents, hold_cents)
    return "captured" if capture_cents == hold_cents else "partially_captured"


def reversal_outcome(status: str) -> HoldStatus:
    """The state a reversal moves the hold to, or an error if it can't."""
    if status != "pending":
        raise InvalidHoldTransition("reverse", status)
    return "reversed"


# The operations. Each one runs in one transaction (hard rule 2), in this order:
#
# 1. Lock the hold row with SELECT ... FOR UPDATE. Any other capture or
#    reversal of the same hold now waits until this transaction ends. That
#    makes "check the state, then change it" safe here: nobody else can change
#    the state between the two.
# 2. Only then look the network ID up. Doing it after the lock matters. Two
#    identical captures arrive together: the second waits on the lock, and
#    when it gets the lock the first has committed. If it had checked the ID
#    before waiting, it would see no capture, then find the hold captured,
#    and return 409 for what is really a retry.
#    Under READ COMMITTED each statement sees everything committed before it
#    starts, and FOR UPDATE returns the latest version of the row once the
#    lock is free, so both reads see the first capture.
# 3. Check the transition and write the capture or reversal row with
#    INSERT ... ON CONFLICT DO NOTHING on the network ID. The lock in step 1
#    is per hold, so it doesn't stop one network ID being reused at the same
#    moment on a different hold. The unique constraint does: the loser gets
#    no row back, rolls back, and is answered from the winner's row.
# 4. Update the hold, and give the released cents back with one
#    UPDATE cards SET available_cents = available_cents + :released.
#    Postgres does the addition, so it can't lose a concurrent authorization's
#    debit (hard rule 3).
# 5. A capture also posts its ledger transaction (services/ledger.py). It
#    only inserts rows, so it takes no lock beyond the ones above.
#
# Lock order is always hold, then card. authorize() locks the card and then
# inserts a new hold, but it never locks an existing hold, so the two can't
# deadlock. expire_holds() follows the same order: holds first, then cards.


def capture(session: Session, request: CaptureRequest) -> CaptureResponse:
    """Keep amount_cents of the authorization's hold and release the rest.

    Idempotent on network_capture_id: a repeat with the same body returns the
    original response, and one with a different body raises IdempotencyConflict.
    """
    try:
        return _capture(session, request)
    except Exception:
        # Ends the transaction and releases the hold's lock straight away,
        # rather than when the request's session is closed.
        session.rollback()
        raise


def reverse(session: Session, request: ReversalRequest) -> ReversalResponse:
    """Release the authorization's whole hold.

    Idempotent on network_reversal_id: a repeat with the same body returns the
    original response, and one with a different body raises IdempotencyConflict.
    """
    try:
        return _reverse(session, request)
    except Exception:
        session.rollback()
        raise


def _capture(session: Session, request: CaptureRequest) -> CaptureResponse:
    hash_hex = request_hash(request)
    hold = _lock_hold(session, request.authorization_id)

    existing = _find_capture(session, request.network_capture_id)
    if existing is not None:
        return _replay_capture(session, existing, hash_hex)
    if hold is None:
        _raise_missing_hold(session, request.authorization_id)

    new_status = capture_outcome(hold.status, hold.amount_cents, request.amount_cents)
    released_cents = hold.amount_cents - request.amount_cents

    capture_id = session.exec(
        insert(CaptureRow)
        .values(
            id=uuid4(),
            network_capture_id=request.network_capture_id,
            request_hash=hash_hex,
            hold_id=hold.id,
            authorization_id=hold.authorization_id,
            amount_cents=request.amount_cents,
            released_cents=released_cents,
        )
        .on_conflict_do_nothing(index_elements=["network_capture_id"])
        .returning(col(CaptureRow.id))
    ).scalar_one_or_none()
    if capture_id is None:
        # The same network_capture_id was committed on another hold while we
        # held this one's lock. Roll back (nothing was written), then answer
        # from the stored row.
        session.rollback()
        original = _find_capture(session, request.network_capture_id)
        assert original is not None  # it just conflicted with us
        return _replay_capture(session, original, hash_hex)

    _settle_hold(session, hold, new_status, request.amount_cents, released_cents)
    # In the same transaction as the capture (hard rule 2): the entries
    # commit with it, or roll back with it if anything below fails.
    ledger.post_capture(session, capture_id, hold.card_id, request.amount_cents)
    session.commit()
    # request.authorization_id, not hold.authorization_id: commit expires
    # loaded rows, and reading one again would run another SELECT.
    return CaptureResponse(
        capture_id=capture_id,
        network_capture_id=request.network_capture_id,
        authorization_id=request.authorization_id,
        amount_cents=request.amount_cents,
        released_cents=released_cents,
        hold_status=new_status,
    )


def _reverse(session: Session, request: ReversalRequest) -> ReversalResponse:
    hash_hex = request_hash(request)
    hold = _lock_hold(session, request.authorization_id)

    existing = _find_reversal(session, request.network_reversal_id)
    if existing is not None:
        return _replay_reversal(session, existing, hash_hex)
    if hold is None:
        _raise_missing_hold(session, request.authorization_id)

    new_status = reversal_outcome(hold.status)
    released_cents = hold.amount_cents

    reversal_id = session.exec(
        insert(ReversalRow)
        .values(
            id=uuid4(),
            network_reversal_id=request.network_reversal_id,
            request_hash=hash_hex,
            hold_id=hold.id,
            authorization_id=hold.authorization_id,
            released_cents=released_cents,
        )
        .on_conflict_do_nothing(index_elements=["network_reversal_id"])
        .returning(col(ReversalRow.id))
    ).scalar_one_or_none()
    if reversal_id is None:
        # See the same branch in _capture.
        session.rollback()
        original = _find_reversal(session, request.network_reversal_id)
        assert original is not None
        return _replay_reversal(session, original, hash_hex)

    _settle_hold(session, hold, new_status, 0, released_cents)
    session.commit()
    return ReversalResponse(
        reversal_id=reversal_id,
        network_reversal_id=request.network_reversal_id,
        authorization_id=request.authorization_id,
        released_cents=released_cents,
        hold_status=new_status,
    )


def expire_holds(session: Session, *, older_than_seconds: int, limit: int = 100) -> int:
    """Expire up to `limit` pending holds older than the window; release their cents.

    One batch, in one transaction. Returns how many holds it expired, so a
    caller can keep going while it gets full batches back.

    Safe to run from many workers at once, and alongside captures and
    reversals:

    1. Claim the oldest pending holds with SELECT ... FOR UPDATE SKIP LOCKED.
       SKIP LOCKED means a hold that another transaction has locked (another
       expiry worker's batch, or a capture or reversal in progress) is left
       out instead of waited for. So two workers always get disjoint batches,
       and neither blocks the other. By the time another worker could claim
       one of our holds, we've committed and it is no longer pending.
       The age is measured with the database's now(), not Python's clock, so
       workers on different machines agree on what's old.
    2. Mark them expired. The status = 'pending' condition is a second guard:
       even if the lock were missing, an UPDATE can only expire a hold that is
       still pending.
    3. Give the cents back with UPDATE cards SET available_cents =
       available_cents + :released, so Postgres does the addition and a
       concurrent authorization's debit can't be lost (hard rule 3). Holds on
       the same card are added together first, and cards are updated in
       sorted id order: two batches that touch the same cards then lock them
       in the same order and can't deadlock each other.

    Nothing is captured, so there is no ledger posting.
    """
    try:
        return _expire_holds(session, older_than_seconds, limit)
    except Exception:
        session.rollback()
        raise


def _expire_holds(session: Session, older_than_seconds: int, limit: int) -> int:
    # func.now() - timedelta(...) becomes now() - interval '...' in SQL.
    cutoff = func.now() - timedelta(seconds=older_than_seconds)
    claimed = session.exec(
        select(HoldRow.id, HoldRow.card_id, HoldRow.amount_cents)
        .where(HoldRow.status == "pending", col(HoldRow.created_at) < cutoff)
        .order_by(col(HoldRow.created_at))
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    if not claimed:
        session.rollback()  # ends the (read-only) transaction
        return 0

    expired_ids = session.exec(
        update(HoldRow)
        .where(
            col(HoldRow.id).in_([hold_id for hold_id, _, _ in claimed]),
            col(HoldRow.status) == "pending",
        )
        .values(status="expired", updated_at=func.now())
        .returning(col(HoldRow.id))
    ).scalars()
    expired = set(expired_ids)
    # Every claimed hold is locked and pending, so all of them are expired.
    # If that ever weren't true, release only what was actually expired.

    # defaultdict(int): a missing card starts at 0, so += just works. This
    # only adds up amounts from the rows we locked; the balance itself is
    # never read here.
    released: defaultdict[UUID, int] = defaultdict(int)
    for hold_id, card_id, amount_cents in claimed:
        if hold_id in expired:
            released[card_id] += amount_cents
    for card_id in sorted(released):
        session.exec(
            update(CardRow)
            .where(col(CardRow.id) == card_id)
            .values(available_cents=col(CardRow.available_cents) + released[card_id])
        )
    session.commit()
    return len(expired)


def _lock_hold(session: Session, authorization_id: UUID) -> HoldRow | None:
    """SELECT ... FOR UPDATE the authorization's hold, or None if it has none.

    The lock lasts until this transaction commits or rolls back.
    """
    return session.exec(
        select(HoldRow)
        .where(HoldRow.authorization_id == authorization_id)
        .with_for_update()
    ).first()


def _settle_hold(
    session: Session,
    hold: HoldRow,
    status: HoldStatus,
    captured_cents: int,
    released_cents: int,
) -> None:
    """Move the (locked) hold to its final state and release cents to the card."""
    session.exec(
        update(HoldRow)
        .where(col(HoldRow.id) == hold.id)
        .values(status=status, captured_cents=captured_cents, updated_at=func.now())
    )
    if released_cents > 0:
        session.exec(
            update(CardRow)
            .where(col(CardRow.id) == hold.card_id)
            .values(available_cents=col(CardRow.available_cents) + released_cents)
        )


def _raise_missing_hold(session: Session, authorization_id: UUID) -> NoReturn:
    """Explain why an authorization has no hold: unknown, or declined.

    NoReturn tells pyright this always raises, so after `if hold is None:
    _raise_missing_hold(...)` it knows hold isn't None.
    """
    decision = session.exec(
        select(AuthorizationRow.decision).where(AuthorizationRow.id == authorization_id)
    ).first()
    if decision is None:
        raise AuthorizationNotFound(authorization_id)
    # Every approval creates a hold in the same transaction (and the migration
    # backfilled older ones), so no hold means it was declined.
    raise AuthorizationNotApproved(authorization_id)


def _find_capture(session: Session, network_capture_id: str) -> CaptureRow | None:
    return session.exec(
        select(CaptureRow).where(CaptureRow.network_capture_id == network_capture_id)
    ).first()


def _find_reversal(session: Session, network_reversal_id: str) -> ReversalRow | None:
    return session.exec(
        select(ReversalRow).where(
            ReversalRow.network_reversal_id == network_reversal_id
        )
    ).first()


def _replay_capture(
    session: Session, original: CaptureRow, hash_hex: str
) -> CaptureResponse:
    """Answer a repeated network_capture_id from the stored capture.

    The response is built before rolling back, because a rollback expires
    loaded rows and reading them again would run another SELECT. Rolling back
    ends the transaction (nothing was written) and frees the hold's lock.
    """
    stored_hash = original.request_hash
    # The hold's state right after this capture follows from the capture
    # itself, so it doesn't need storing: nothing released means all of it
    # was captured.
    status: HoldStatus = (
        "captured" if original.released_cents == 0 else "partially_captured"
    )
    response = CaptureResponse(
        capture_id=original.id,
        network_capture_id=original.network_capture_id,
        authorization_id=original.authorization_id,
        amount_cents=original.amount_cents,
        released_cents=original.released_cents,
        hold_status=status,
    )
    session.rollback()
    if stored_hash != hash_hex:
        raise IdempotencyConflict(response.network_capture_id)
    return response


def _replay_reversal(
    session: Session, original: ReversalRow, hash_hex: str
) -> ReversalResponse:
    """Answer a repeated network_reversal_id from the stored reversal."""
    stored_hash = original.request_hash
    response = ReversalResponse(
        reversal_id=original.id,
        network_reversal_id=original.network_reversal_id,
        authorization_id=original.authorization_id,
        released_cents=original.released_cents,
        hold_status="reversed",
    )
    session.rollback()
    if stored_hash != hash_hex:
        raise IdempotencyConflict(response.network_reversal_id)
    return response
