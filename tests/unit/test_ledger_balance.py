"""The ledger's balance rule and account lists, checked without a database."""

from typing import get_args
from uuid import uuid4

import pytest

from authz.models import (
    LEDGER_ACCOUNTS,
    LEDGER_TRANSACTION_KINDS,
    LedgerEntryRow,
    LedgerTransactionRow,
)
from authz.services.ledger import (
    Entry,
    LedgerAccount,
    LedgerTransactionKind,
    UnbalancedTransaction,
    capture_entries,
    check_balanced,
)


def test_model_and_service_list_the_same_accounts_and_kinds() -> None:
    assert set(get_args(LedgerAccount)) == set(LEDGER_ACCOUNTS)
    assert set(get_args(LedgerTransactionKind)) == set(LEDGER_TRANSACTION_KINDS)


def _check_sql(table: type[LedgerEntryRow] | type[LedgerTransactionRow]) -> str:
    """The text of every CHECK constraint on the model's table."""
    return " ".join(
        str(c.sqltext)  # pyright: ignore[reportAttributeAccessIssue]
        for c in table.__table__.constraints  # pyright: ignore[reportAttributeAccessIssue]
        if c.__class__.__name__ == "CheckConstraint"
    )


def test_check_constraints_name_every_account_and_kind() -> None:
    for account in LEDGER_ACCOUNTS:
        assert f"'{account}'" in _check_sql(LedgerEntryRow)
    for kind in LEDGER_TRANSACTION_KINDS:
        assert f"'{kind}'" in _check_sql(LedgerTransactionRow)


def test_capture_entries_move_the_amount_from_card_to_merchant() -> None:
    card_id = uuid4()
    assert capture_entries(card_id, 1_234) == [
        Entry(account="card_spend", amount_cents=-1_234, card_id=card_id),
        Entry(account="merchant_settlement", amount_cents=1_234),
    ]


def test_capture_entries_are_balanced() -> None:
    check_balanced(capture_entries(uuid4(), 1))  # doesn't raise


def test_more_than_two_entries_can_balance() -> None:
    check_balanced(
        [
            Entry(account="card_spend", amount_cents=-300, card_id=uuid4()),
            Entry(account="merchant_settlement", amount_cents=100),
            Entry(account="merchant_settlement", amount_cents=200),
        ]
    )


@pytest.mark.parametrize(
    "amounts",
    [
        [-100, 99],  # off by one
        [100, 100],  # both sides positive
        [-100],  # one-sided
        [],  # empty
        [0, 0],  # sums to zero but moves nothing
        [-100, 100, 0],  # a zero line among balanced ones
    ],
)
def test_invalid_entries_are_rejected(amounts: list[int]) -> None:
    entries = [Entry(account="merchant_settlement", amount_cents=a) for a in amounts]
    with pytest.raises(UnbalancedTransaction):
        check_balanced(entries)
