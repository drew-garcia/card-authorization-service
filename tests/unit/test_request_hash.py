"""request_hash tells a retry (same body) from a reused id (different body)."""

from typing import Any

import pytest

from authz.schemas import AuthorizationRequest
from authz.services.authorizations import request_hash

BODY: dict[str, Any] = {
    "network_request_id": "req-1",
    "card_id": "3f2b8c1e-9a4d-4e6f-8b7a-1c2d3e4f5a6b",
    "amount_cents": 1250,
    "currency": "USD",
    "merchant_name": "Corner Coffee",
    "merchant_category_code": "5814",
}


def hash_of(body: dict[str, Any]) -> str:
    return request_hash(AuthorizationRequest.model_validate(body))


def test_hash_is_sha256_hex_and_stable() -> None:
    first = hash_of(BODY)
    assert len(first) == 64
    assert int(first, 16) >= 0  # parses as hex
    assert hash_of(dict(BODY)) == first


def test_key_order_does_not_change_hash() -> None:
    reversed_body = dict(reversed(list(BODY.items())))
    assert hash_of(reversed_body) == hash_of(BODY)


def test_stripped_whitespace_does_not_change_hash() -> None:
    # str_strip_whitespace on the model trims this before it is hashed.
    assert hash_of({**BODY, "merchant_name": "  Corner Coffee "}) == hash_of(BODY)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("network_request_id", "req-2"),
        ("card_id", "00000000-0000-4000-8000-000000000000"),
        ("amount_cents", 1251),
        ("merchant_name", "Corner Cafe"),
        ("merchant_category_code", "5812"),
    ],
)
def test_any_field_change_changes_hash(field: str, value: Any) -> None:
    assert hash_of({**BODY, field: value}) != hash_of(BODY)
