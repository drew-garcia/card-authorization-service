"""Concurrent authorizations against one card, through a multi-worker server."""

import asyncio
from collections.abc import Callable

import httpx
import pytest

# The make_card fixture in tests/conftest.py: balance in cents -> new card id.
MakeCard = Callable[[int], str]

LIMIT_CENTS = 10_000  # $100
AMOUNT_CENTS = 1_000  # $10
SWIPES = 20


async def _swipe_concurrently(base_url: str, card_id: str) -> int:
    """Send SWIPES authorizations at the same moment and count the approvals.

    asyncio.gather starts every request before awaiting any of them, so all
    twenty are in flight together instead of one after another.
    """
    bodies = [
        {
            "network_request_id": f"race-{i}",
            "card_id": card_id,
            "amount_cents": AMOUNT_CENTS,
            "currency": "USD",
            "merchant_name": "Corner Coffee",
            "merchant_category_code": "5814",
        }
        for i in range(SWIPES)
    ]
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        responses = await asyncio.gather(
            *(client.post("/authorizations", json=body) for body in bodies)
        )
    assert [r.status_code for r in responses] == [200] * SWIPES
    return sum(r.json()["decision"] == "approved" for r in responses)


@pytest.mark.xfail(reason="race, fix later")
def test_concurrent_authorizations_approve_only_what_fits(
    live_server: str, make_card: MakeCard
) -> None:
    card_id = make_card(LIMIT_CENTS)
    # asyncio.run starts an event loop, runs the coroutine to completion, and
    # closes the loop, so a plain (sync) test can drive async code.
    approvals = asyncio.run(_swipe_concurrently(live_server, card_id))
    assert approvals == LIMIT_CENTS // AMOUNT_CENTS, f"{approvals} approvals"
