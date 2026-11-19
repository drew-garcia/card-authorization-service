from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient


def test_create_card_returns_201_with_id_and_balance(client: TestClient) -> None:
    response = client.post("/cards", json={"initial_balance_cents": 1000})
    assert response.status_code == 201
    body = response.json()
    UUID(body["id"])  # raises if not a UUID
    assert body["available_cents"] == 1000


def test_get_card_round_trips(client: TestClient) -> None:
    created = client.post("/cards", json={"initial_balance_cents": 1000}).json()
    response = client.get(f"/cards/{created['id']}")
    assert response.status_code == 200
    assert response.json() == created


def test_unknown_card_returns_404(client: TestClient) -> None:
    response = client.get(f"/cards/{uuid4()}")
    assert response.status_code == 404


def test_non_uuid_card_id_returns_422(client: TestClient) -> None:
    response = client.get("/cards/not-a-uuid")
    assert response.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"initial_balance_cents": -1},
        {"initial_balance_cents": 10.0},
        {"initial_balance_cents": "10"},
        {"initial_balance_cents": True},
        {},
        {"initial_balance_cents": 10, "surprise": "hi"},
    ],
)
def test_invalid_create_body_returns_422(
    client: TestClient, body: dict[str, Any]
) -> None:
    response = client.post("/cards", json=body)
    assert response.status_code == 422
