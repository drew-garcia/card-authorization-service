"""Several expiry workers at once, and expiry racing captures and reversals.

The done criterion for the expiry worker: two workers running at once never
release the same hold twice. A double release shows up three ways, and each
test checks all of them: the workers' counts add up to more than the number
of holds, a card ends with more available than its limit, and the invariant
checker finds available != limit - pending holds - captured.

The workers here are threads, each with its own database connection: to
Postgres that's the same as separate processes, and psycopg releases the GIL
while it waits on the database, so their transactions really do overlap. A
Barrier starts them at the same moment.
"""

import asyncio
import random
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
from sqlalchemy import Engine, text
from sqlmodel import Session

from authz.invariants import check_invariants
from authz.services.holds import expire_holds

MakeCard = Callable[[int], str]
MakeHold = Callable[[str, int, int], str]

CARDS = 10
HOLDS_PER_CARD = 100
HOLD_CENTS = 100
LIMIT_CENTS = HOLDS_PER_CARD * HOLD_CENTS * 2
WINDOW_SECONDS = 3_600
OLD = WINDOW_SECONDS + 60
WORKERS = 4
# Small batches, so each worker runs many transactions and they interleave.
BATCH = 10


def _drain(
    engine: Engine,
    start: threading.Barrier,
    counts: list[int],
    batch: int,
    pause_seconds: float,
) -> None:
    """One worker: expire batches until there's nothing left, like run_once."""
    start.wait()
    while True:
        with Session(engine) as s:
            n = expire_holds(s, older_than_seconds=WINDOW_SECONDS, limit=batch)
        if n == 0:
            return
        counts.append(n)
        time.sleep(pause_seconds)


def _run_workers(
    engine: Engine, batch: int = BATCH, pause_seconds: float = 0
) -> list[list[int]]:
    """Run WORKERS drains at once; return each worker's batch sizes."""
    start = threading.Barrier(WORKERS)
    per_worker: list[list[int]] = [[] for _ in range(WORKERS)]
    threads = [
        threading.Thread(
            target=_drain,
            args=(engine, start, per_worker[i], batch, pause_seconds),
        )
        for i in range(WORKERS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "a worker hung"
    return per_worker


def _available(engine: Engine) -> dict[str, tuple[int, int]]:
    """card id -> (limit, available)."""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, limit_cents, available_cents FROM cards"))
        return {str(card_id): (limit, avail) for card_id, limit, avail in rows}


def _statuses(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT status, COUNT(*) FROM holds GROUP BY status"))
        return {status: n for status, n in rows}


def test_concurrent_workers_release_each_hold_once(
    engine: Engine, make_card: MakeCard, make_hold: MakeHold
) -> None:
    cards = [make_card(LIMIT_CENTS) for _ in range(CARDS)]
    for card_id in cards:
        for _ in range(HOLDS_PER_CARD):
            make_hold(card_id, HOLD_CENTS, OLD)
    total_holds = CARDS * HOLDS_PER_CARD

    per_worker = _run_workers(engine)

    # Every hold was expired by exactly one worker.
    assert sum(sum(batches) for batches in per_worker) == total_holds
    assert _statuses(engine) == {"expired": total_holds}
    # Each card got its holds back once: not more than it had to begin with.
    assert all(avail == limit for limit, avail in _available(engine).values())
    with Session(engine) as s:
        assert check_invariants(s) == []
    # The workers really overlapped: the work was shared, not done by one
    # thread while the others found nothing.
    assert sum(1 for batches in per_worker if batches) >= 2


async def _settle_all(
    base_url: str, requests: list[tuple[str, dict[str, Any]]]
) -> list[httpx.Response]:
    async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
        return await asyncio.gather(
            *(client.post(path, json=body) for path, body in requests)
        )


def test_expiry_racing_captures_and_reversals_settles_each_hold_once(
    live_server: str, engine: Engine, make_card: MakeCard, make_hold: MakeHold
) -> None:
    """Every old hold is captured, reversed or expired: exactly one of them.

    Expiry skips holds a capture or reversal has locked; a capture or reversal
    that waited on a hold expiry had locked then finds it expired (409). Either
    way the money moves once.
    """
    rng = random.Random(20261128)
    cards = [make_card(LIMIT_CENTS) for _ in range(CARDS)]
    # Each hold is a second older than the one before it. Expiry takes the
    # oldest first, so it works from the end of `requests` while the HTTP
    # burst starts at the front: the two sides meet somewhere in the middle
    # and fight over the same holds there, whichever one is faster.
    targets = [card_id for card_id in cards for _ in range(HOLDS_PER_CARD // 4)]
    rng.shuffle(targets)
    holds = [
        make_hold(card_id, HOLD_CENTS, OLD + i) for i, card_id in enumerate(targets)
    ]
    requests: list[tuple[str, dict[str, Any]]] = []
    for i, authorization_id in enumerate(holds):
        if rng.random() < 0.5:
            requests.append(
                (
                    "/captures",
                    {
                        "network_capture_id": f"cap-{i}",
                        "authorization_id": authorization_id,
                        "amount_cents": rng.randint(1, HOLD_CENTS),
                    },
                )
            )
        else:
            requests.append(
                (
                    "/reversals",
                    {
                        "network_reversal_id": f"rev-{i}",
                        "authorization_id": authorization_id,
                    },
                )
            )

    # Left at full speed, the workers expire every hold before the first
    # HTTP request lands, and nothing races. One hold per batch with a short
    # pause stretches the drain over roughly the same time as the burst.
    expired_counts: list[list[int]] = []
    workers = threading.Thread(
        target=lambda: expired_counts.extend(
            _run_workers(engine, batch=1, pause_seconds=0.02)
        )
    )
    workers.start()
    responses = asyncio.run(_settle_all(live_server, requests))
    workers.join(timeout=60)
    assert not workers.is_alive()

    assert all(r.status_code in (200, 409) for r in responses)
    settled_by_request = sum(1 for r in responses if r.status_code == 200)
    expired = sum(sum(batches) for batches in expired_counts)
    # Each hold went exactly one way; none were left pending or settled twice.
    assert settled_by_request + expired == len(holds)
    # Both sides won some holds, so this really was a race and not one side
    # finishing before the other started.
    assert settled_by_request > 0
    assert expired > 0
    statuses = _statuses(engine)
    assert statuses.get("pending", 0) == 0
    assert statuses.get("expired", 0) == expired
    with engine.connect() as conn:
        settled_rows = conn.execute(
            text(
                "SELECT (SELECT COUNT(*) FROM captures) + (SELECT COUNT(*) FROM reversals)"
            )
        ).scalar_one()
    assert settled_rows == settled_by_request
    assert all(avail <= limit for limit, avail in _available(engine).values())
    with Session(engine) as s:
        assert check_invariants(s) == []
