"""The double-entry ledger: posting balanced transactions.

Every money movement is one ledger transaction made of signed entries that
sum to zero (hard rule 5). For a capture of X cents:

    card_spend           -X   (card_id = the card)  the card spent X
    merchant_settlement  +X   (no card)             the merchant is owed X

Merchants aren't stored yet, so merchant_settlement is one shared system
account. Holds and reversals post nothing: a hold only sets money aside on
the card, and a reversal gives it back, so no money reaches the merchant.

Posting never commits. It runs inside the caller's transaction, so the
entries commit or roll back together with the capture that caused them
(hard rule 2).
"""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import insert
from sqlmodel import Session

from authz.models import LedgerEntryRow, LedgerTransactionRow

LedgerAccount = Literal["card_spend", "merchant_settlement"]
LedgerTransactionKind = Literal["capture"]


class UnbalancedTransaction(Exception):
    """A ledger transaction's entries don't sum to zero, or it has none."""


# frozen=True makes instances immutable (and hashable), which suits a value
# that only describes a line to write.
@dataclass(frozen=True)
class Entry:
    """One line of a ledger transaction, before it is written."""

    account: LedgerAccount
    amount_cents: int
    card_id: UUID | None = None


def check_balanced(entries: list[Entry]) -> None:
    """Raise UnbalancedTransaction unless the entries form a valid posting.

    A pure function, so the unit tests can check it without a database. The
    database checks the same thing again at commit (see the migration's
    constraint trigger); this catches it earlier, with a clearer error.
    """
    if len(entries) < 2:
        raise UnbalancedTransaction("a ledger transaction needs at least two entries")
    if any(e.amount_cents == 0 for e in entries):
        raise UnbalancedTransaction("ledger entries can't be zero")
    total = sum(e.amount_cents for e in entries)
    if total != 0:
        raise UnbalancedTransaction(f"ledger entries sum to {total}, not 0")


def capture_entries(card_id: UUID, amount_cents: int) -> list[Entry]:
    """The two entries a capture of amount_cents on card_id posts."""
    return [
        Entry(account="card_spend", amount_cents=-amount_cents, card_id=card_id),
        Entry(account="merchant_settlement", amount_cents=amount_cents),
    ]


def post_capture(
    session: Session, capture_id: UUID, card_id: UUID, amount_cents: int
) -> UUID:
    """Post the ledger transaction for a capture, and return its id.

    Call it inside the capture's transaction, before its commit.
    """
    return _post(session, "capture", capture_id, capture_entries(card_id, amount_cents))


def _post(
    session: Session,
    kind: LedgerTransactionKind,
    capture_id: UUID,
    entries: list[Entry],
) -> UUID:
    check_balanced(entries)
    transaction_id = uuid4()
    # Core inserts, like the rest of the services: they run straight away in
    # the current transaction, rather than waiting for the ORM to flush.
    session.exec(
        insert(LedgerTransactionRow).values(
            id=transaction_id, kind=kind, capture_id=capture_id
        )
    )
    # A list of dicts makes one multi-row INSERT for all the entries.
    session.exec(
        insert(LedgerEntryRow).values(
            [
                {
                    "id": uuid4(),
                    "ledger_transaction_id": transaction_id,
                    "account": e.account,
                    "card_id": e.card_id,
                    "amount_cents": e.amount_cents,
                }
                for e in entries
            ]
        )
    )
    return transaction_id
