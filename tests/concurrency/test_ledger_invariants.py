"""1,000 random concurrent operations, then the money must still add up.

Authorizations, captures, reversals, retries and reused network IDs are fired
at a multi-worker server in ten waves of 100 at once. Captures and reversals
need authorization IDs, so each wave works on the approvals of the waves
before it. Afterwards the invariant checker must find nothing wrong: every
ledger transaction sums to zero, and every card's
available = limit - pending holds - captured.

Waves of 100 rather than one burst of 1,000: each uvicorn worker's pool
opens up to 15 connections, and a much bigger burst churns them fast enough
to hit Postgres's default limit of 100 clients, which is a 500 that has
nothing to do with the ledger.

The random generator is seeded, so a failure can be replayed exactly; the
interleaving inside Postgres still differs from run to run.
"""

import asyncio
import random
from collections import Counter
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import Engine, text
from sqlmodel import Session

from authz.invariants import check_invariants

# The make_card fixture in tests/conftest.py: balance in cents -> new card id.
MakeCard = Callable[[int], str]

SEED = 20261127
CARDS = 10
WAVES = 10
OPS_PER_WAVE = 100
# Few hot holds per wave, so many requests fight over the same hold's lock.
HOT_HOLDS = 10

Request = tuple[str, dict[str, Any]]


class Scenario:
    """Builds random requests and remembers what earlier waves created."""

    def __init__(self, rng: random.Random, card_ids: list[str]) -> None:
        self.rng = rng
        self.card_ids = card_ids
        self.counter = 0
        # authorization_id -> amount, for approvals from finished waves.
        self.approved: dict[str, int] = {}
        self.declined: list[str] = []
        self.sent: list[Request] = []

    def _id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}-{self.counter}"

    def authorization(self) -> Request:
        # 1 in 20 swipes is for a card that doesn't exist.
        card_id = (
            str(uuid4()) if self.rng.random() < 0.05 else self.rng.choice(self.card_ids)
        )
        return (
            "/authorizations",
            {
                "network_request_id": self._id("req"),
                "card_id": card_id,
                # Big enough that busy cards run out and start declining.
                "amount_cents": self.rng.randint(100, 6_000),
                "currency": "USD",
                "merchant_name": "Corner Coffee",
                "merchant_category_code": "5814",
            },
        )

    def _target(self, hot: list[str]) -> tuple[str, int]:
        """An authorization to settle: mostly a hot hold, sometimes odd ones."""
        roll = self.rng.random()
        if roll < 0.05:
            return str(uuid4()), 100  # unknown: 404
        if roll < 0.10 and self.declined:
            return self.rng.choice(self.declined), 100  # declined: 409
        authorization_id = self.rng.choice(hot)
        return authorization_id, self.approved[authorization_id]

    def capture(self, hot: list[str]) -> Request:
        authorization_id, hold_cents = self._target(hot)
        roll = self.rng.random()
        if roll < 0.4:
            amount = hold_cents  # full
        elif roll < 0.9:
            amount = self.rng.randint(1, hold_cents)  # partial (or full)
        else:
            amount = hold_cents + self.rng.randint(1, 500)  # too much: 409
        return (
            "/captures",
            {
                "network_capture_id": self._id("cap"),
                "authorization_id": authorization_id,
                "amount_cents": amount,
            },
        )

    def reversal(self, hot: list[str]) -> Request:
        authorization_id, _ = self._target(hot)
        return (
            "/reversals",
            {
                "network_reversal_id": self._id("rev"),
                "authorization_id": authorization_id,
            },
        )

    def reused_id(self, request: Request) -> Request:
        """The same network ID with a different body: a 409 once the first wins."""
        path, body = request
        changed = dict(body)
        if "amount_cents" in changed:
            changed["amount_cents"] += 1
        else:
            # A reversal's only other field is the authorization.
            changed["authorization_id"] = str(uuid4())
        return path, changed

    def wave(self) -> list[Request]:
        hot = self.rng.sample(list(self.approved), min(HOT_HOLDS, len(self.approved)))
        requests: list[Request] = []
        while len(requests) < OPS_PER_WAVE:
            roll = self.rng.random()
            earlier = self.sent + requests
            # The first wave has nothing to settle yet: swipes and retries.
            if roll < 0.35 or (not hot and roll < 0.85):
                requests.append(self.authorization())
            elif roll < 0.65:
                requests.append(self.capture(hot))
            elif roll < 0.85:
                requests.append(self.reversal(hot))
            elif roll < 0.95 and earlier:
                # An exact retry: concurrent with the original if it is in
                # this wave, a late replay if it was in an earlier one.
                requests.append(self.rng.choice(earlier))
            elif earlier:
                requests.append(self.reused_id(self.rng.choice(earlier)))
        self.rng.shuffle(requests)
        self.sent.extend(requests)
        return requests

    def record(self, requests: list[Request], responses: list[httpx.Response]) -> None:
        for (path, _), response in zip(requests, responses, strict=True):
            if path != "/authorizations" or response.status_code != 200:
                continue
            body = response.json()
            if body["decision"] == "approved":
                self.approved[body["authorization_id"]] = body["amount_cents"]
            else:
                self.declined.append(body["authorization_id"])


async def _post_all(base_url: str, requests: list[Request]) -> list[httpx.Response]:
    """Send every (path, body) at the same moment."""
    async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
        return await asyncio.gather(
            *(client.post(path, json=body) for path, body in requests)
        )


def _unbalanced_transactions(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT ledger_transaction_id, SUM(amount_cents)"
                    " FROM ledger_entries GROUP BY ledger_transaction_id"
                    " HAVING SUM(amount_cents) <> 0"
                )
            ).all()
        )


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT COUNT(*) FROM {table}")).one()[0]


def test_ledger_and_balances_add_up_after_1000_random_operations(
    live_server: str, make_card: MakeCard, engine: Engine, session: Session
) -> None:
    rng = random.Random(SEED)
    card_ids = [make_card(rng.randint(5_000, 50_000)) for _ in range(CARDS)]
    scenario = Scenario(rng, card_ids)

    statuses: Counter[tuple[str, int]] = Counter()
    for _ in range(WAVES):
        requests = scenario.wave()
        responses = asyncio.run(_post_all(live_server, requests))
        scenario.record(requests, responses)
        for (path, _), response in zip(requests, responses, strict=True):
            statuses[(path, response.status_code)] += 1

    assert len(scenario.sent) == WAVES * OPS_PER_WAVE == 1_000
    # Only expected answers: no 500s, and no 422s from badly built requests.
    assert {code for _, code in statuses} <= {200, 404, 409}, statuses

    # The scenario really did exercise each path, so passing means something.
    assert statuses[("/captures", 200)] > 0, statuses
    assert statuses[("/captures", 409)] > 0, statuses
    assert statuses[("/reversals", 200)] > 0, statuses
    assert scenario.declined, "no swipe was declined"
    assert _count(engine, "ledger_transactions") == _count(engine, "captures") > 0

    # The two rules this feature is done on, stated directly...
    assert _unbalanced_transactions(engine) == []
    # ...and every other money rule, through the shared checker.
    assert check_invariants(session) == []
