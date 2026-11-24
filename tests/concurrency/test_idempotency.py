"""Concurrent repeats of one network_request_id, through a multi-worker server."""

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine, func
from sqlmodel import Session, select

from authz.models import AuthorizationRow

# The make_card fixture in tests/conftest.py: balance in cents -> new card id.
MakeCard = Callable[[int], str]

BALANCE_CENTS = 10_000
AMOUNT_CENTS = 1_000
REPEATS = 50


def _body(card_id: str, amount_cents: int = AMOUNT_CENTS) -> dict[str, Any]:
    return {
        "network_request_id": "retry-1",
        "card_id": card_id,
        "amount_cents": amount_cents,
        "currency": "USD",
        "merchant_name": "Corner Coffee",
        "merchant_category_code": "5814",
    }


async def _post_all(
    base_url: str, bodies: list[dict[str, Any]]
) -> list[httpx.Response]:
    """Send every body at the same moment (asyncio.gather starts them all first)."""
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        return await asyncio.gather(
            *(client.post("/authorizations", json=body) for body in bodies)
        )


def _row_count(engine: Engine) -> int:
    with Session(engine) as s:
        return s.exec(select(func.count()).select_from(AuthorizationRow)).one()


# existing_card: repeats queue on the card's row lock, then on the unique key.
# unknown_card: no card row to lock, so only the unique key serializes them.
@pytest.mark.parametrize("card", ["existing_card", "unknown_card"])
def test_concurrent_identical_requests_create_one_row(
    live_server: str, make_card: MakeCard, engine: Engine, card: str
) -> None:
    card_id = make_card(BALANCE_CENTS) if card == "existing_card" else str(uuid4())
    responses = asyncio.run(_post_all(live_server, [_body(card_id)] * REPEATS))

    assert [r.status_code for r in responses] == [200] * REPEATS
    first = responses[0].json()
    assert all(r.json() == first for r in responses)
    assert _row_count(engine) == 1
    if card == "existing_card":
        assert first["decision"] == "approved"
        balance = httpx.get(f"{live_server}/cards/{card_id}").json()["available_cents"]
        assert balance == BALANCE_CENTS - AMOUNT_CENTS
    else:
        assert first["decline_reason"] == "card_not_found"


def test_concurrent_conflicting_bodies_one_wins_the_rest_get_409(
    live_server: str, make_card: MakeCard, engine: Engine
) -> None:
    card_id = make_card(BALANCE_CENTS)
    half = REPEATS // 2
    # Interleaved so neither body has a head start in the gather order.
    bodies = [_body(card_id, AMOUNT_CENTS + i % 2) for i in range(REPEATS)]
    responses = asyncio.run(_post_all(live_server, bodies))

    ok = [
        (b, r) for b, r in zip(bodies, responses, strict=True) if r.status_code == 200
    ]
    conflicts = [r for r in responses if r.status_code == 409]
    assert len(ok) == half and len(conflicts) == half

    # Every 200 is for the winning body and carries the same response.
    winner_amounts = {b["amount_cents"] for b, _ in ok}
    assert len(winner_amounts) == 1
    assert all(r.json() == ok[0][1].json() for _, r in ok)
    assert _row_count(engine) == 1
    balance = httpx.get(f"{live_server}/cards/{card_id}").json()["available_cents"]
    assert balance == BALANCE_CENTS - winner_amounts.pop()
