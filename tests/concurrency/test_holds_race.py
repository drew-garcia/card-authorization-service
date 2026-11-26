"""Concurrent captures and reversals of one hold, through a multi-worker server."""

import asyncio
from collections.abc import Callable
from typing import Any

import httpx
from sqlalchemy import Engine, func
from sqlmodel import Session, select

from authz.models import CaptureRow, HoldRow, ReversalRow

# The make_card fixture in tests/conftest.py: balance in cents -> new card id.
MakeCard = Callable[[int], str]

BALANCE_CENTS = 10_000
HOLD_CENTS = 3_000
CAPTURE_CENTS = 1_000
REPEATS = 50


def _approve(base_url: str, card_id: str) -> str:
    response = httpx.post(
        f"{base_url}/authorizations",
        json={
            "network_request_id": "req-1",
            "card_id": card_id,
            "amount_cents": HOLD_CENTS,
            "currency": "USD",
            "merchant_name": "Corner Coffee",
            "merchant_category_code": "5814",
        },
    )
    assert response.json()["decision"] == "approved"
    return response.json()["authorization_id"]


async def _post_all(
    base_url: str, requests: list[tuple[str, dict[str, Any]]]
) -> list[httpx.Response]:
    """Send every (path, body) at the same moment."""
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        return await asyncio.gather(
            *(client.post(path, json=body) for path, body in requests)
        )


def _balance(base_url: str, card_id: str) -> int:
    return httpx.get(f"{base_url}/cards/{card_id}").json()["available_cents"]


def _count(engine: Engine, model: type[CaptureRow] | type[ReversalRow]) -> int:
    with Session(engine) as s:
        return s.exec(select(func.count()).select_from(model)).one()


def _hold_status(engine: Engine, authorization_id: str) -> str:
    with Session(engine) as s:
        return s.exec(
            select(HoldRow.status).where(HoldRow.authorization_id == authorization_id)
        ).one()


def test_concurrent_identical_captures_capture_once(
    live_server: str, make_card: MakeCard, engine: Engine
) -> None:
    """Retries queue on the hold's lock, then each finds the first capture.

    If the network ID were checked before taking the lock, the waiting
    retries would find the hold already captured and get 409s.
    """
    card_id = make_card(BALANCE_CENTS)
    authorization_id = _approve(live_server, card_id)
    body = {
        "network_capture_id": "cap-1",
        "authorization_id": authorization_id,
        "amount_cents": CAPTURE_CENTS,
    }
    responses = asyncio.run(_post_all(live_server, [("/captures", body)] * REPEATS))

    assert [r.status_code for r in responses] == [200] * REPEATS
    assert all(r.json() == responses[0].json() for r in responses)
    assert _count(engine, CaptureRow) == 1
    assert _hold_status(engine, authorization_id) == "partially_captured"
    # The 2,000 cents not captured were released once, not fifty times.
    assert _balance(live_server, card_id) == BALANCE_CENTS - CAPTURE_CENTS


def test_concurrent_capture_and_reversal_one_wins(
    live_server: str, make_card: MakeCard, engine: Engine
) -> None:
    """Different captures and reversals race for one hold: exactly one wins."""
    card_id = make_card(BALANCE_CENTS)
    authorization_id = _approve(live_server, card_id)
    requests: list[tuple[str, dict[str, Any]]] = []
    # Interleaved, so neither kind has a head start in the gather order.
    for i in range(REPEATS // 2):
        requests.append(
            (
                "/captures",
                {
                    "network_capture_id": f"cap-{i}",
                    "authorization_id": authorization_id,
                    "amount_cents": CAPTURE_CENTS,
                },
            )
        )
        requests.append(
            (
                "/reversals",
                {
                    "network_reversal_id": f"rev-{i}",
                    "authorization_id": authorization_id,
                },
            )
        )
    responses = asyncio.run(_post_all(live_server, requests))

    winners = [
        (path, r)
        for (path, _), r in zip(requests, responses, strict=True)
        if r.status_code == 200
    ]
    assert len(winners) == 1
    assert sorted(r.status_code for r in responses) == [200] + [409] * (REPEATS - 1)
    assert _count(engine, CaptureRow) + _count(engine, ReversalRow) == 1

    path, _ = winners[0]
    if path == "/captures":
        assert _hold_status(engine, authorization_id) == "partially_captured"
        assert _balance(live_server, card_id) == BALANCE_CENTS - CAPTURE_CENTS
    else:
        assert _hold_status(engine, authorization_id) == "reversed"
        assert _balance(live_server, card_id) == BALANCE_CENTS


def test_releases_and_new_authorizations_on_one_card_add_up(
    live_server: str, make_card: MakeCard, engine: Engine
) -> None:
    """Releases add to available_cents while new swipes subtract from it.

    Both run as one UPDATE each on the card row, so none is lost.
    """
    card_id = make_card(BALANCE_CENTS)
    holds = []
    for i in range(3):
        response = httpx.post(
            f"{live_server}/authorizations",
            json={
                "network_request_id": f"hold-{i}",
                "card_id": card_id,
                "amount_cents": HOLD_CENTS,
                "currency": "USD",
                "merchant_name": "Corner Coffee",
                "merchant_category_code": "5814",
            },
        )
        holds.append(response.json()["authorization_id"])
    # 10,000 - 3 * 3,000 = 1,000 left.
    requests: list[tuple[str, dict[str, Any]]] = [
        ("/reversals", {"network_reversal_id": f"rev-{i}", "authorization_id": a})
        for i, a in enumerate(holds)
    ] + [
        (
            "/authorizations",
            {
                "network_request_id": f"swipe-{i}",
                "card_id": card_id,
                "amount_cents": 100,
                "currency": "USD",
                "merchant_name": "Corner Coffee",
                "merchant_category_code": "5814",
            },
        )
        for i in range(10)
    ]
    responses = asyncio.run(_post_all(live_server, requests))

    assert all(r.status_code == 200 for r in responses)
    # 1,000 left covers all ten 100-cent swipes whatever the order.
    swipes = responses[3:]
    assert all(r.json()["decision"] == "approved" for r in swipes)
    assert _balance(live_server, card_id) == BALANCE_CENTS - 10 * 100
