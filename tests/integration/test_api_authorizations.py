from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from authz.schemas import BIGINT_MAX

# The make_card fixture in conftest.py: balance in cents -> new card id.
MakeCard = Callable[[int], str]


def valid_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "network_request_id": "req-123",
        "card_id": "3f2b8c1e-9a4d-4e6f-8b7a-1c2d3e4f5a6b",
        "amount_cents": 1250,
        "currency": "USD",
        "merchant_name": "Corner Coffee",
        "merchant_category_code": "5814",
    }
    body.update(overrides)
    return body


def balance(client: TestClient, card_id: str) -> int:
    return client.get(f"/cards/{card_id}").json()["available_cents"]


def test_approve_subtracts_amount(client: TestClient, make_card: MakeCard) -> None:
    card_id = make_card(1000)
    response = client.post(
        "/authorizations", json=valid_body(card_id=card_id, amount_cents=600)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "approved"
    assert body["decline_reason"] is None
    assert body["card_id"] == card_id
    assert body["amount_cents"] == 600
    assert body["network_request_id"] == "req-123"
    assert balance(client, card_id) == 400


def test_exact_balance_is_approved(client: TestClient, make_card: MakeCard) -> None:
    card_id = make_card(1000)
    response = client.post(
        "/authorizations", json=valid_body(card_id=card_id, amount_cents=1000)
    )
    assert response.json()["decision"] == "approved"
    assert balance(client, card_id) == 0


def test_over_balance_declines_and_keeps_balance(
    client: TestClient, make_card: MakeCard
) -> None:
    card_id = make_card(1000)
    response = client.post(
        "/authorizations", json=valid_body(card_id=card_id, amount_cents=1001)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "declined"
    assert body["decline_reason"] == "insufficient_funds"
    assert balance(client, card_id) == 1000


# The conditional UPDATE matches no row for these, so the service must still
# find the card and say insufficient_funds, not card_not_found or a 500.
@pytest.mark.parametrize(
    ("balance_cents", "amount_cents"),
    [(0, 1), (1000, BIGINT_MAX)],
    ids=["empty_card", "amount_at_bigint_max"],
)
def test_existing_card_that_cannot_cover_declines_insufficient_funds(
    client: TestClient, make_card: MakeCard, balance_cents: int, amount_cents: int
) -> None:
    card_id = make_card(balance_cents)
    response = client.post(
        "/authorizations", json=valid_body(card_id=card_id, amount_cents=amount_cents)
    )
    assert response.status_code == 200
    assert response.json()["decline_reason"] == "insufficient_funds"
    assert balance(client, card_id) == balance_cents


def test_second_swipe_declines_once_balance_is_spent(
    client: TestClient, make_card: MakeCard
) -> None:
    card_id = make_card(1000)
    first = client.post(
        "/authorizations",
        json=valid_body(network_request_id="a", card_id=card_id, amount_cents=600),
    )
    second = client.post(
        "/authorizations",
        json=valid_body(network_request_id="b", card_id=card_id, amount_cents=600),
    )
    assert first.json()["decision"] == "approved"
    assert second.json()["decline_reason"] == "insufficient_funds"
    assert balance(client, card_id) == 400


def test_unknown_card_declines_with_card_not_found(client: TestClient) -> None:
    response = client.post("/authorizations", json=valid_body(card_id=str(uuid4())))
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "declined"
    assert body["decline_reason"] == "card_not_found"


@pytest.mark.parametrize(
    ("field", "value"),
    [("amount_cents", -100), ("merchant_category_code", "123")],
)
def test_invalid_body_returns_422(client: TestClient, field: str, value: Any) -> None:
    response = client.post("/authorizations", json=valid_body(**{field: value}))
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", field]


# Idempotency on network_request_id (hard rule 4).


def test_repeat_with_same_body_returns_original_and_debits_once(
    client: TestClient, make_card: MakeCard
) -> None:
    card_id = make_card(1000)
    body = valid_body(card_id=card_id, amount_cents=600)
    first = client.post("/authorizations", json=body)
    second = client.post("/authorizations", json=body)
    assert first.status_code == second.status_code == 200
    assert second.json() == first.json()
    assert balance(client, card_id) == 400


def test_repeat_after_balance_is_spent_still_returns_original_approval(
    client: TestClient, make_card: MakeCard
) -> None:
    # Deciding again would decline (only 400 left); the original was approved.
    card_id = make_card(1000)
    body = valid_body(card_id=card_id, amount_cents=600)
    first = client.post("/authorizations", json=body).json()
    second = client.post("/authorizations", json=body).json()
    assert first["decision"] == "approved"
    assert second == first
    assert balance(client, card_id) == 400


def test_repeat_of_declined_unknown_card_returns_same_decline(
    client: TestClient,
) -> None:
    body = valid_body(card_id=str(uuid4()))
    first = client.post("/authorizations", json=body).json()
    second = client.post("/authorizations", json=body).json()
    assert first["decline_reason"] == "card_not_found"
    assert second == first


def test_repeat_with_different_body_returns_409_and_changes_nothing(
    client: TestClient, make_card: MakeCard
) -> None:
    card_id = make_card(1000)
    body = valid_body(card_id=card_id, amount_cents=600)
    original = client.post("/authorizations", json=body).json()

    conflict = client.post("/authorizations", json={**body, "amount_cents": 100})
    assert conflict.status_code == 409
    assert "different request body" in conflict.json()["detail"]
    # The 409's own debit was rolled back.
    assert balance(client, card_id) == 400

    # The original body still replays after the conflict.
    replay = client.post("/authorizations", json=body)
    assert replay.status_code == 200
    assert replay.json() == original
