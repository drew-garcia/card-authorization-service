from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


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


def make_card(client: TestClient, balance_cents: int) -> str:
    response = client.post("/cards", json={"initial_balance_cents": balance_cents})
    return response.json()["id"]


def balance(client: TestClient, card_id: str) -> int:
    return client.get(f"/cards/{card_id}").json()["available_cents"]


def test_approve_subtracts_amount(client: TestClient) -> None:
    card_id = make_card(client, 1000)
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


def test_exact_balance_is_approved(client: TestClient) -> None:
    card_id = make_card(client, 1000)
    response = client.post(
        "/authorizations", json=valid_body(card_id=card_id, amount_cents=1000)
    )
    assert response.json()["decision"] == "approved"
    assert balance(client, card_id) == 0


def test_over_balance_declines_and_keeps_balance(client: TestClient) -> None:
    card_id = make_card(client, 1000)
    response = client.post(
        "/authorizations", json=valid_body(card_id=card_id, amount_cents=1001)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "declined"
    assert body["decline_reason"] == "insufficient_funds"
    assert balance(client, card_id) == 1000


def test_second_swipe_declines_once_balance_is_spent(client: TestClient) -> None:
    card_id = make_card(client, 1000)
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
