from typing import Any

import pytest
from fastapi.testclient import TestClient

from authz.main import app

client = TestClient(app)


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


def test_valid_body_reaches_stub() -> None:
    response = client.post("/authorizations", json=valid_body())
    assert response.status_code == 501


@pytest.mark.parametrize(
    ("field", "value"),
    [("amount_cents", -100), ("merchant_category_code", "123")],
)
def test_invalid_body_returns_422(field: str, value: Any) -> None:
    response = client.post("/authorizations", json=valid_body(**{field: value}))
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", field]
