"""Holds: placed by approvals, settled by POST /captures and POST /reversals."""

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, text
from sqlmodel import Session, select

from authz.models import CaptureRow, HoldRow, ReversalRow

# The make_card fixture in tests/conftest.py: balance in cents -> new card id.
MakeCard = Callable[[int], str]

BALANCE_CENTS = 10_000
HOLD_CENTS = 3_000


def auth_body(card_id: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "network_request_id": "req-1",
        "card_id": card_id,
        "amount_cents": HOLD_CENTS,
        "currency": "USD",
        "merchant_name": "Corner Coffee",
        "merchant_category_code": "5814",
    }
    body.update(overrides)
    return body


def capture_body(authorization_id: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "network_capture_id": "cap-1",
        "authorization_id": authorization_id,
        "amount_cents": HOLD_CENTS,
    }
    body.update(overrides)
    return body


def reversal_body(authorization_id: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "network_reversal_id": "rev-1",
        "authorization_id": authorization_id,
    }
    body.update(overrides)
    return body


def approve(client: TestClient, card_id: str, **overrides: Any) -> str:
    """Approve HOLD_CENTS on the card and return the authorization_id."""
    response = client.post("/authorizations", json=auth_body(card_id, **overrides))
    assert response.json()["decision"] == "approved"
    return response.json()["authorization_id"]


def balance(client: TestClient, card_id: str) -> int:
    return client.get(f"/cards/{card_id}").json()["available_cents"]


def hold_of(engine: Engine, authorization_id: str) -> HoldRow | None:
    # A fresh session each time, so nothing is served from a stale cache.
    with Session(engine) as s:
        return s.exec(
            select(HoldRow).where(HoldRow.authorization_id == authorization_id)
        ).first()


def hold_state(engine: Engine, authorization_id: str) -> tuple[Any, ...]:
    """Plain values to compare before and after (ORM rows don't compare well)."""
    hold = hold_of(engine, authorization_id)
    assert hold is not None
    return (hold.status, hold.captured_cents, hold.updated_at)


def count(engine: Engine, model: type[CaptureRow] | type[ReversalRow]) -> int:
    with Session(engine) as s:
        return s.exec(select(func.count()).select_from(model)).one()


@pytest.fixture
def card_id(make_card: MakeCard) -> str:
    return make_card(BALANCE_CENTS)


@pytest.fixture
def authorization_id(client: TestClient, card_id: str) -> str:
    return approve(client, card_id)


# Approvals place holds.


def test_approval_places_a_pending_hold(
    client: TestClient, engine: Engine, card_id: str, authorization_id: str
) -> None:
    hold = hold_of(engine, authorization_id)
    assert hold is not None
    assert hold.status == "pending"
    assert hold.amount_cents == HOLD_CENTS
    assert hold.captured_cents == 0
    assert str(hold.card_id) == card_id
    assert balance(client, card_id) == BALANCE_CENTS - HOLD_CENTS


def test_decline_places_no_hold(client: TestClient, engine: Engine) -> None:
    response = client.post("/authorizations", json=auth_body(str(uuid4())))
    assert response.json()["decision"] == "declined"
    assert hold_of(engine, response.json()["authorization_id"]) is None


def test_repeated_authorization_places_one_hold(
    client: TestClient, engine: Engine, card_id: str, authorization_id: str
) -> None:
    repeat = client.post("/authorizations", json=auth_body(card_id))
    assert repeat.json()["authorization_id"] == authorization_id
    with Session(engine) as s:
        assert s.exec(select(func.count()).select_from(HoldRow)).one() == 1


# Valid moves out of pending.


def test_full_capture_keeps_the_whole_hold(
    client: TestClient, engine: Engine, card_id: str, authorization_id: str
) -> None:
    response = client.post("/captures", json=capture_body(authorization_id))
    assert response.status_code == 200
    body = response.json()
    assert body["hold_status"] == "captured"
    assert body["amount_cents"] == HOLD_CENTS
    assert body["released_cents"] == 0
    assert body["authorization_id"] == authorization_id
    assert body["network_capture_id"] == "cap-1"

    hold = hold_of(engine, authorization_id)
    assert hold is not None
    assert (hold.status, hold.captured_cents) == ("captured", HOLD_CENTS)
    assert balance(client, card_id) == BALANCE_CENTS - HOLD_CENTS


def test_partial_capture_releases_the_rest(
    client: TestClient, engine: Engine, card_id: str, authorization_id: str
) -> None:
    response = client.post(
        "/captures", json=capture_body(authorization_id, amount_cents=1_200)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["hold_status"] == "partially_captured"
    assert body["amount_cents"] == 1_200
    assert body["released_cents"] == HOLD_CENTS - 1_200

    hold = hold_of(engine, authorization_id)
    assert hold is not None
    assert (hold.status, hold.captured_cents) == ("partially_captured", 1_200)
    assert balance(client, card_id) == BALANCE_CENTS - 1_200


def test_reversal_releases_the_whole_hold(
    client: TestClient, engine: Engine, card_id: str, authorization_id: str
) -> None:
    response = client.post("/reversals", json=reversal_body(authorization_id))
    assert response.status_code == 200
    body = response.json()
    assert body["hold_status"] == "reversed"
    assert body["released_cents"] == HOLD_CENTS
    assert body["network_reversal_id"] == "rev-1"

    hold = hold_of(engine, authorization_id)
    assert hold is not None
    assert (hold.status, hold.captured_cents) == ("reversed", 0)
    assert balance(client, card_id) == BALANCE_CENTS


def test_released_money_can_be_authorized_again(
    client: TestClient, card_id: str, authorization_id: str
) -> None:
    client.post("/reversals", json=reversal_body(authorization_id))
    again = client.post(
        "/authorizations",
        json=auth_body(card_id, network_request_id="req-2", amount_cents=BALANCE_CENTS),
    )
    assert again.json()["decision"] == "approved"
    assert balance(client, card_id) == 0


# Invalid moves: every final state, against both actions.


def settle(
    client: TestClient, engine: Engine, authorization_id: str, state: str
) -> None:
    """Move a pending hold into a final state, using different network IDs
    from the ones the test then sends."""
    if state == "captured":
        r = client.post(
            "/captures", json=capture_body(authorization_id, network_capture_id="setup")
        )
    elif state == "partially_captured":
        r = client.post(
            "/captures",
            json=capture_body(
                authorization_id, network_capture_id="setup", amount_cents=1_000
            ),
        )
    elif state == "reversed":
        r = client.post(
            "/reversals",
            json=reversal_body(authorization_id, network_reversal_id="setup"),
        )
    else:
        # Nothing expires holds yet (that's the expiry worker's job), so set
        # it directly. A real expiry would also release the cents; the tests
        # below only compare the balance before and after the rejected move.
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE holds SET status = :s WHERE authorization_id = :a"),
                {"s": state, "a": authorization_id},
            )
        return
    assert r.status_code == 200


FINAL_STATES = ["captured", "partially_captured", "reversed", "expired"]


@pytest.mark.parametrize("state", FINAL_STATES)
def test_capture_of_a_settled_hold_is_409(
    client: TestClient,
    engine: Engine,
    card_id: str,
    authorization_id: str,
    state: str,
) -> None:
    settle(client, engine, authorization_id, state)
    before = (balance(client, card_id), hold_state(engine, authorization_id))

    response = client.post("/captures", json=capture_body(authorization_id))
    assert response.status_code == 409
    assert response.json()["detail"] == f"cannot capture a hold that is {state}"
    assert (balance(client, card_id), hold_state(engine, authorization_id)) == before
    assert count(engine, CaptureRow) == (1 if "captured" in state else 0)


@pytest.mark.parametrize("state", FINAL_STATES)
def test_reversal_of_a_settled_hold_is_409(
    client: TestClient,
    engine: Engine,
    card_id: str,
    authorization_id: str,
    state: str,
) -> None:
    settle(client, engine, authorization_id, state)
    before = (balance(client, card_id), hold_state(engine, authorization_id))

    response = client.post("/reversals", json=reversal_body(authorization_id))
    assert response.status_code == 409
    assert response.json()["detail"] == f"cannot reverse a hold that is {state}"
    assert (balance(client, card_id), hold_state(engine, authorization_id)) == before
    assert count(engine, ReversalRow) == (1 if state == "reversed" else 0)


def test_capture_above_the_hold_is_409_and_changes_nothing(
    client: TestClient, engine: Engine, card_id: str, authorization_id: str
) -> None:
    response = client.post(
        "/captures", json=capture_body(authorization_id, amount_cents=HOLD_CENTS + 1)
    )
    assert response.status_code == 409
    assert "exceeds the hold" in response.json()["detail"]
    hold = hold_of(engine, authorization_id)
    assert hold is not None and hold.status == "pending"
    assert balance(client, card_id) == BALANCE_CENTS - HOLD_CENTS
    assert count(engine, CaptureRow) == 0


@pytest.mark.parametrize(
    ("path", "make_body"), [("/captures", capture_body), ("/reversals", reversal_body)]
)
def test_unknown_authorization_is_404(
    client: TestClient, path: str, make_body: Callable[[str], dict[str, Any]]
) -> None:
    response = client.post(path, json=make_body(str(uuid4())))
    assert response.status_code == 404
    assert response.json()["detail"] == "authorization not found"


@pytest.mark.parametrize(
    ("path", "make_body"), [("/captures", capture_body), ("/reversals", reversal_body)]
)
def test_declined_authorization_is_409(
    client: TestClient, path: str, make_body: Callable[[str], dict[str, Any]]
) -> None:
    declined = client.post("/authorizations", json=auth_body(str(uuid4()))).json()
    response = client.post(path, json=make_body(declined["authorization_id"]))
    assert response.status_code == 409
    assert "declined" in response.json()["detail"]


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/captures", {"network_capture_id": "c", "amount_cents": 0}),
        ("/captures", {"network_capture_id": "c", "amount_cents": 1.5}),
        ("/reversals", {"network_reversal_id": ""}),
    ],
)
def test_invalid_body_is_422(
    client: TestClient, authorization_id: str, path: str, body: dict[str, Any]
) -> None:
    response = client.post(path, json={"authorization_id": authorization_id, **body})
    assert response.status_code == 422


# Idempotency on network_capture_id and network_reversal_id (hard rule 4).


@pytest.mark.parametrize("amount_cents", [HOLD_CENTS, 1_200], ids=["full", "partial"])
def test_duplicate_capture_returns_original_and_releases_once(
    client: TestClient,
    engine: Engine,
    card_id: str,
    authorization_id: str,
    amount_cents: int,
) -> None:
    body = capture_body(authorization_id, amount_cents=amount_cents)
    first = client.post("/captures", json=body)
    # The hold is no longer pending, so only the idempotency check can make
    # this a 200 rather than a 409.
    second = client.post("/captures", json=body)
    assert first.status_code == second.status_code == 200
    assert second.json() == first.json()
    assert count(engine, CaptureRow) == 1
    assert balance(client, card_id) == BALANCE_CENTS - amount_cents


def test_reused_capture_id_with_different_body_is_409(
    client: TestClient, engine: Engine, card_id: str, authorization_id: str
) -> None:
    body = capture_body(authorization_id, amount_cents=1_200)
    original = client.post("/captures", json=body).json()

    conflict = client.post("/captures", json={**body, "amount_cents": 1_300})
    assert conflict.status_code == 409
    assert "network_capture_id" in conflict.json()["detail"]
    assert balance(client, card_id) == BALANCE_CENTS - 1_200

    replay = client.post("/captures", json=body)
    assert replay.json() == original


def test_reused_capture_id_on_another_authorization_is_409(
    client: TestClient, engine: Engine, card_id: str, authorization_id: str
) -> None:
    other = approve(client, card_id, network_request_id="req-2")
    client.post("/captures", json=capture_body(authorization_id))

    response = client.post("/captures", json=capture_body(other))
    assert response.status_code == 409
    hold = hold_of(engine, other)
    assert hold is not None and hold.status == "pending"


def test_reused_capture_id_on_unknown_authorization_is_409_not_404(
    client: TestClient, authorization_id: str
) -> None:
    # The ID check comes first: a reused ID is a conflict whatever it points at.
    client.post("/captures", json=capture_body(authorization_id))
    response = client.post("/captures", json=capture_body(str(uuid4())))
    assert response.status_code == 409
    assert "network_capture_id" in response.json()["detail"]


def test_duplicate_reversal_returns_original_and_releases_once(
    client: TestClient, engine: Engine, card_id: str, authorization_id: str
) -> None:
    body = reversal_body(authorization_id)
    first = client.post("/reversals", json=body)
    second = client.post("/reversals", json=body)
    assert first.status_code == second.status_code == 200
    assert second.json() == first.json()
    assert count(engine, ReversalRow) == 1
    assert balance(client, card_id) == BALANCE_CENTS


def test_reused_reversal_id_with_different_body_is_409(
    client: TestClient, engine: Engine, card_id: str, authorization_id: str
) -> None:
    other = approve(client, card_id, network_request_id="req-2")
    client.post("/reversals", json=reversal_body(authorization_id))

    response = client.post("/reversals", json=reversal_body(other))
    assert response.status_code == 409
    assert "network_reversal_id" in response.json()["detail"]
    hold = hold_of(engine, other)
    assert hold is not None and hold.status == "pending"
    assert balance(client, card_id) == BALANCE_CENTS - HOLD_CENTS


def test_capture_and_reversal_ids_are_separate_namespaces(
    client: TestClient, card_id: str, authorization_id: str
) -> None:
    # The same string as a capture ID and a reversal ID are two different keys.
    other = approve(client, card_id, network_request_id="req-2")
    cap = client.post(
        "/captures", json=capture_body(authorization_id, network_capture_id="x")
    )
    rev = client.post("/reversals", json=reversal_body(other, network_reversal_id="x"))
    assert cap.status_code == rev.status_code == 200
