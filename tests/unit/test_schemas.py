from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from authz.schemas import BIGINT_MAX, AuthorizationRequest

CARD_ID = "3f2b8c1e-9a4d-4e6f-8b7a-1c2d3e4f5a6b"


def valid_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "network_request_id": "req-123",
        "card_id": CARD_ID,
        "amount_cents": 1250,
        "currency": "USD",
        "merchant_name": "Corner Coffee",
        "merchant_category_code": "5814",
    }
    payload.update(overrides)
    return payload


def test_valid_payload_parses() -> None:
    req = AuthorizationRequest.model_validate(valid_payload())
    assert req.card_id == UUID(CARD_ID)
    assert req.amount_cents == 1250
    assert req.merchant_category_code == "5814"


def test_mcc_keeps_leading_zero() -> None:
    req = AuthorizationRequest.model_validate(
        valid_payload(merchant_category_code="0742")
    )
    assert req.merchant_category_code == "0742"


@pytest.mark.parametrize("amount", [1, BIGINT_MAX])
def test_amount_boundaries_pass(amount: int) -> None:
    req = AuthorizationRequest.model_validate(valid_payload(amount_cents=amount))
    assert req.amount_cents == amount


def test_merchant_name_at_max_length_passes() -> None:
    AuthorizationRequest.model_validate(valid_payload(merchant_name="x" * 100))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount_cents", 0),
        ("amount_cents", 100.0),  # a whole-number float; lax mode would accept it
        ("amount_cents", "100"),
        ("amount_cents", True),
        ("amount_cents", BIGINT_MAX + 1),
        ("currency", "usd"),
        ("merchant_category_code", "123"),
        ("merchant_category_code", "12345"),
        ("merchant_category_code", "12a4"),
        ("merchant_category_code", "١٢٣٤"),  # Arabic-Indic digits
        ("merchant_category_code", 5411),
        ("card_id", "not-a-uuid"),
        ("merchant_name", "   "),  # only fails because whitespace is stripped first
        ("merchant_name", "x" * 101),
        ("network_request_id", ""),
        ("network_request_id", "x" * 65),
    ],
)
def test_invalid_field_rejected(field: str, value: Any) -> None:
    with pytest.raises(ValidationError) as exc:
        AuthorizationRequest.model_validate(valid_payload(**{field: value}))
    assert exc.value.errors()[0]["loc"] == (field,)


def test_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        AuthorizationRequest.model_validate(valid_payload(surprise="hi"))


@pytest.mark.parametrize("field", list(valid_payload()))
def test_missing_field_rejected(field: str) -> None:
    payload = valid_payload()
    del payload[field]
    with pytest.raises(ValidationError):
        AuthorizationRequest.model_validate(payload)


def test_request_is_immutable() -> None:
    req = AuthorizationRequest.model_validate(valid_payload())
    with pytest.raises(ValidationError):
        req.amount_cents = 1  # pyright: ignore[reportAttributeAccessIssue]
