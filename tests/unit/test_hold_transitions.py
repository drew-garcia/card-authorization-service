"""The hold state rules, checked for every state, without a database."""

from typing import Any, get_args

import pytest
from pydantic import ValidationError

from authz.models import HOLD_STATUSES
from authz.schemas import (
    BIGINT_MAX,
    CaptureRequest,
    HoldStatus,
    ReversalRequest,
)
from authz.services.holds import (
    CaptureExceedsHold,
    InvalidHoldTransition,
    capture_outcome,
    reversal_outcome,
)

FINAL_STATES = ["captured", "partially_captured", "reversed", "expired"]
AUTHORIZATION_ID = "3f2b8c1e-9a4d-4e6f-8b7a-1c2d3e4f5a6b"


def test_model_and_schema_list_the_same_states() -> None:
    # get_args pulls the strings out of the Literal type.
    assert set(get_args(HoldStatus)) == set(HOLD_STATUSES)
    assert set(FINAL_STATES) | {"pending"} == set(HOLD_STATUSES)


# Valid moves.


def test_capturing_the_whole_hold_is_captured() -> None:
    assert capture_outcome("pending", 1000, 1000) == "captured"


@pytest.mark.parametrize("capture_cents", [1, 999])
def test_capturing_part_of_the_hold_is_partially_captured(capture_cents: int) -> None:
    assert capture_outcome("pending", 1000, capture_cents) == "partially_captured"


def test_reversing_a_pending_hold_is_reversed() -> None:
    assert reversal_outcome("pending") == "reversed"


# Invalid moves.


def test_capturing_more_than_the_hold_is_rejected() -> None:
    with pytest.raises(CaptureExceedsHold):
        capture_outcome("pending", 1000, 1001)


@pytest.mark.parametrize("status", FINAL_STATES)
def test_capturing_a_settled_hold_is_rejected(status: str) -> None:
    with pytest.raises(InvalidHoldTransition) as info:
        capture_outcome(status, 1000, 1000)
    assert info.value.status == status
    assert str(info.value) == f"cannot capture a hold that is {status}"


@pytest.mark.parametrize("status", FINAL_STATES)
def test_reversing_a_settled_hold_is_rejected(status: str) -> None:
    with pytest.raises(InvalidHoldTransition) as info:
        reversal_outcome(status)
    assert str(info.value) == f"cannot reverse a hold that is {status}"


# Request schemas.


def capture_payload(**overrides: Any) -> dict[str, Any]:
    return {
        "network_capture_id": "cap-1",
        "authorization_id": AUTHORIZATION_ID,
        "amount_cents": 500,
        **overrides,
    }


@pytest.mark.parametrize("amount", [1, BIGINT_MAX])
def test_capture_amount_boundaries_pass(amount: int) -> None:
    req = CaptureRequest.model_validate(capture_payload(amount_cents=amount))
    assert req.amount_cents == amount


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount_cents", 0),
        ("amount_cents", -1),
        ("amount_cents", BIGINT_MAX + 1),
        ("amount_cents", 5.0),
        ("amount_cents", "500"),
        ("network_capture_id", ""),
        ("network_capture_id", "x" * 65),
        ("authorization_id", "not-a-uuid"),
        ("surprise", 1),
    ],
)
def test_invalid_capture_is_rejected(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        CaptureRequest.model_validate(capture_payload(**{field: value}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("network_reversal_id", ""),
        ("authorization_id", "not-a-uuid"),
        # A reversal always releases the whole hold, so it takes no amount.
        ("amount_cents", 500),
    ],
)
def test_invalid_reversal_is_rejected(field: str, value: Any) -> None:
    payload = {"network_reversal_id": "rev-1", "authorization_id": AUTHORIZATION_ID}
    with pytest.raises(ValidationError):
        ReversalRequest.model_validate({**payload, field: value})
