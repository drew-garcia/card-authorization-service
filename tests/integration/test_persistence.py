"""Checks on what the API writes to the database, read back with SQL."""

from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from authz.models import AuthorizationRow, CardRow


def auth_body(
    card_id: str, amount_cents: int, request_id: str = "req-1"
) -> dict[str, object]:
    return {
        "network_request_id": request_id,
        "card_id": card_id,
        "amount_cents": amount_cents,
        "currency": "USD",
        "merchant_name": "Corner Coffee",
        "merchant_category_code": "5814",
    }


def make_card(client: TestClient, balance_cents: int) -> str:
    return client.post("/cards", json={"initial_balance_cents": balance_cents}).json()[
        "id"
    ]


def stored_card(session: Session, card_id: str) -> CardRow:
    row = session.get(CardRow, UUID(card_id))
    assert row is not None
    return row


def stored_authorizations(session: Session) -> list[AuthorizationRow]:
    return list(session.exec(select(AuthorizationRow)).all())


def test_new_card_limit_equals_available(client: TestClient, session: Session) -> None:
    card = stored_card(session, make_card(client, 1000))
    assert card.limit_cents == 1000
    assert card.available_cents == 1000


def test_approval_is_recorded(client: TestClient, session: Session) -> None:
    card_id = make_card(client, 1000)
    response = client.post("/authorizations", json=auth_body(card_id, 600)).json()

    [row] = stored_authorizations(session)
    assert row.id == UUID(response["authorization_id"])
    assert row.network_request_id == "req-1"
    assert row.card_id == UUID(card_id)
    assert row.amount_cents == 600
    assert row.decision == "approved"
    assert row.decline_reason is None
    assert row.created_at is not None
    assert stored_card(session, card_id).available_cents == 400
    # The limit is fixed; only the available balance moves.
    assert stored_card(session, card_id).limit_cents == 1000


def test_insufficient_funds_decline_is_recorded(
    client: TestClient, session: Session
) -> None:
    card_id = make_card(client, 1000)
    client.post("/authorizations", json=auth_body(card_id, 1001))

    [row] = stored_authorizations(session)
    assert row.decision == "declined"
    assert row.decline_reason == "insufficient_funds"
    assert stored_card(session, card_id).available_cents == 1000


def test_unknown_card_decline_is_recorded(client: TestClient, session: Session) -> None:
    missing = str(uuid4())
    client.post("/authorizations", json=auth_body(missing, 500))

    [row] = stored_authorizations(session)
    assert row.card_id == UUID(missing)
    assert row.decision == "declined"
    assert row.decline_reason == "card_not_found"
    assert session.get(CardRow, UUID(missing)) is None


def test_balance_above_32_bit_range_round_trips(client: TestClient) -> None:
    # 2**31 cents is past what a 32-bit INTEGER column can hold.
    card_id = make_card(client, 2**31)
    assert client.get(f"/cards/{card_id}").json()["available_cents"] == 2**31
