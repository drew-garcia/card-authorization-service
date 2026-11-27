"""The ledger: what captures post, and the database rules that guard it."""

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from authz.invariants import check_invariants

# The make_card fixture in tests/conftest.py: balance in cents -> new card id.
MakeCard = Callable[[int], str]

BALANCE_CENTS = 10_000
HOLD_CENTS = 3_000


def approve(client: TestClient, card_id: str, request_id: str = "req-1") -> str:
    response = client.post(
        "/authorizations",
        json={
            "network_request_id": request_id,
            "card_id": card_id,
            "amount_cents": HOLD_CENTS,
            "currency": "USD",
            "merchant_name": "Corner Coffee",
            "merchant_category_code": "5814",
        },
    )
    assert response.json()["decision"] == "approved"
    return response.json()["authorization_id"]


def capture(
    client: TestClient,
    authorization_id: str,
    amount_cents: int = HOLD_CENTS,
    capture_id: str = "cap-1",
) -> Any:
    return client.post(
        "/captures",
        json={
            "network_capture_id": capture_id,
            "authorization_id": authorization_id,
            "amount_cents": amount_cents,
        },
    )


def entries(engine: Engine) -> list[tuple[str, str, str | None, int]]:
    """(transaction kind, account, card_id, amount) for every entry, in order."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT t.kind, e.account, e.card_id, e.amount_cents"
                " FROM ledger_entries e"
                " JOIN ledger_transactions t ON t.id = e.ledger_transaction_id"
                " ORDER BY e.amount_cents"
            )
        ).all()
    return [(k, a, None if c is None else str(c), n) for k, a, c, n in rows]


def transaction_count(engine: Engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM ledger_transactions")).one()[0]


@pytest.fixture
def card_id(make_card: MakeCard) -> str:
    return make_card(BALANCE_CENTS)


@pytest.fixture
def authorization_id(client: TestClient, card_id: str) -> str:
    return approve(client, card_id)


# What captures post.


def test_full_capture_posts_card_spend_and_merchant_settlement(
    client: TestClient, engine: Engine, card_id: str, authorization_id: str
) -> None:
    assert capture(client, authorization_id).status_code == 200
    assert entries(engine) == [
        ("capture", "card_spend", card_id, -HOLD_CENTS),
        ("capture", "merchant_settlement", None, HOLD_CENTS),
    ]


def test_partial_capture_posts_only_the_captured_amount(
    client: TestClient, engine: Engine, card_id: str, authorization_id: str
) -> None:
    assert capture(client, authorization_id, amount_cents=1_000).status_code == 200
    assert entries(engine) == [
        ("capture", "card_spend", card_id, -1_000),
        ("capture", "merchant_settlement", None, 1_000),
    ]


def test_ledger_transaction_points_at_its_capture(
    client: TestClient, engine: Engine, authorization_id: str
) -> None:
    capture_id = capture(client, authorization_id).json()["capture_id"]
    with engine.connect() as conn:
        linked = conn.execute(text("SELECT capture_id FROM ledger_transactions")).all()
    assert [str(r[0]) for r in linked] == [capture_id]


def test_repeated_capture_posts_once(
    client: TestClient, engine: Engine, authorization_id: str
) -> None:
    first = capture(client, authorization_id)
    again = capture(client, authorization_id)
    assert again.json() == first.json()
    assert transaction_count(engine) == 1


def test_captures_on_two_cards_post_to_each_card(
    client: TestClient, engine: Engine, make_card: MakeCard
) -> None:
    card_a, card_b = make_card(BALANCE_CENTS), make_card(BALANCE_CENTS)
    capture(client, approve(client, card_a, "req-a"), 1_000, "cap-a")
    capture(client, approve(client, card_b, "req-b"), 2_000, "cap-b")
    card_side = {c: n for _, a, c, n in entries(engine) if a == "card_spend"}
    assert card_side == {card_a: -1_000, card_b: -2_000}


# What posts nothing.


def test_approval_and_reversal_post_nothing(
    client: TestClient, engine: Engine, authorization_id: str
) -> None:
    response = client.post(
        "/reversals",
        json={"network_reversal_id": "rev-1", "authorization_id": authorization_id},
    )
    assert response.status_code == 200
    assert transaction_count(engine) == 0


def test_rejected_captures_post_nothing(
    client: TestClient, engine: Engine, authorization_id: str
) -> None:
    # Above the hold, then an unknown authorization, then a settled hold.
    assert capture(client, authorization_id, HOLD_CENTS + 1).status_code == 409
    assert capture(client, str(uuid4()), capture_id="cap-x").status_code == 404
    assert capture(client, authorization_id, capture_id="cap-ok").status_code == 200
    assert capture(client, authorization_id, capture_id="cap-2").status_code == 409
    assert transaction_count(engine) == 1


def test_reused_capture_id_with_different_body_posts_nothing_more(
    client: TestClient, engine: Engine, authorization_id: str
) -> None:
    capture(client, authorization_id, amount_cents=1_000)
    assert capture(client, authorization_id, amount_cents=2_000).status_code == 409
    assert [n for *_, n in entries(engine)] == [-1_000, 1_000]


# The database's own guards, independent of the service code.


def _insert_entries(session: Session, amounts: list[int]) -> None:
    """Write a ledger transaction with merchant_settlement entries directly.

    It needs a real capture to point at, so it borrows the only one there is.
    """
    txn_id = uuid4()
    session.execute(
        text(
            "INSERT INTO ledger_transactions (id, kind, capture_id)"
            " SELECT :id, 'capture', id FROM captures"
        ),
        {"id": txn_id},
    )
    for amount in amounts:
        session.execute(
            text(
                "INSERT INTO ledger_entries"
                " (id, ledger_transaction_id, account, amount_cents)"
                " VALUES (:id, :txn, 'merchant_settlement', :amount)"
            ),
            {"id": uuid4(), "txn": txn_id, "amount": amount},
        )


@pytest.fixture
def unposted_capture(engine: Engine, client: TestClient, authorization_id: str) -> None:
    """A capture whose ledger posting was removed, so a test can post its own.

    The trigger also runs on DELETE, so the entries and the transaction go in
    one transaction, leaving nothing unbalanced at commit.
    """
    capture(client, authorization_id)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ledger_entries"))
        conn.execute(text("DELETE FROM ledger_transactions"))


@pytest.mark.usefixtures("unposted_capture")
def test_database_refuses_to_commit_an_unbalanced_transaction(engine: Engine) -> None:
    with Session(engine) as s:
        # Each INSERT succeeds: the check is deferred to COMMIT.
        _insert_entries(s, [-HOLD_CENTS, HOLD_CENTS - 1])
        with pytest.raises(IntegrityError, match="unbalanced"):
            s.commit()
    assert transaction_count(engine) == 0


@pytest.mark.usefixtures("unposted_capture")
def test_database_accepts_a_balanced_transaction(engine: Engine) -> None:
    with Session(engine) as s:
        _insert_entries(s, [-HOLD_CENTS, HOLD_CENTS])
        s.commit()
    assert transaction_count(engine) == 1


def test_database_refuses_to_unbalance_a_committed_transaction(
    engine: Engine, client: TestClient, authorization_id: str
) -> None:
    capture(client, authorization_id)
    with Session(engine) as s:
        s.execute(
            text(
                "UPDATE ledger_entries SET amount_cents = amount_cents + 1"
                " WHERE account = 'merchant_settlement'"
            )
        )
        with pytest.raises(IntegrityError, match="unbalanced"):
            s.commit()


def test_card_spend_entry_needs_a_card(
    engine: Engine, client: TestClient, authorization_id: str
) -> None:
    capture(client, authorization_id)
    with (
        Session(engine) as s,
        pytest.raises(IntegrityError, match="card_matches_account"),
    ):
        s.execute(text("UPDATE ledger_entries SET card_id = NULL"))


def test_capture_posts_at_most_one_ledger_transaction(
    engine: Engine, client: TestClient, authorization_id: str
) -> None:
    capture(client, authorization_id)
    with (
        Session(engine) as s,
        pytest.raises(IntegrityError, match="uq_ledger_transactions_capture_id"),
    ):
        _insert_entries(s, [-1, 1])


# The invariant checker.


def test_invariants_hold_after_a_mix_of_operations(
    client: TestClient, session: Session, make_card: MakeCard
) -> None:
    card = make_card(BALANCE_CENTS)
    held = approve(client, card, "req-held")
    capture(client, approve(client, card, "req-full"), capture_id="cap-full")
    capture(client, approve(client, card, "req-part"), 1_000, "cap-part")
    client.post(
        "/reversals",
        json={"network_reversal_id": "rev-1", "authorization_id": held},
    )
    assert check_invariants(session) == []


def test_invariants_catch_a_tampered_balance(
    client: TestClient, engine: Engine, session: Session, authorization_id: str
) -> None:
    capture(client, authorization_id, amount_cents=1_000)
    with engine.begin() as conn:
        conn.execute(text("UPDATE cards SET available_cents = available_cents + 1"))
    problems = check_invariants(session)
    assert len(problems) == 1
    assert "available" in problems[0]


def test_invariants_catch_a_capture_with_no_ledger_transaction(
    client: TestClient, session: Session, unposted_capture: None
) -> None:
    problems = check_invariants(session)
    # The capture is unposted, and its card's ledger and holds disagree.
    assert any("0 ledger transactions" in p for p in problems)
    assert any("ledger shows 0 cents captured" in p for p in problems)
