"""Hold expiry: expire_holds() and the worker loop around it, on real Postgres."""

import threading
import time
from collections.abc import Callable
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlmodel import Session, select

from authz.invariants import check_invariants
from authz.models import CardRow, HoldRow
from authz.services.holds import expire_holds
from authz.settings import Settings
from authz.workers.hold_expiry import run, run_once

MakeCard = Callable[[int], str]
MakeHold = Callable[[str, int, int], str]

BALANCE_CENTS = 100_000
HOLD_CENTS = 3_000
WINDOW_SECONDS = 3_600
OLD = WINDOW_SECONDS + 60
YOUNG = WINDOW_SECONDS - 60


def expire(engine: Engine, limit: int = 100) -> int:
    with Session(engine) as s:
        return expire_holds(s, older_than_seconds=WINDOW_SECONDS, limit=limit)


def balance(engine: Engine, card_id: str) -> int:
    with Session(engine) as s:
        card = s.get(CardRow, UUID(card_id))
        assert card is not None
        return card.available_cents


def status(engine: Engine, authorization_id: str) -> str:
    with Session(engine) as s:
        return s.exec(
            select(HoldRow.status).where(
                HoldRow.authorization_id == UUID(authorization_id)
            )
        ).one()


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "unused",
        "hold_expiry_seconds": WINDOW_SECONDS,
        "hold_expiry_interval_seconds": 0.01,
        "hold_expiry_batch_size": 100,
    }
    values.update(overrides)
    return Settings(**values)  # pyright: ignore[reportArgumentType]


def test_old_pending_hold_is_expired_and_released(
    engine: Engine, make_card: MakeCard, make_hold: MakeHold
) -> None:
    card_id = make_card(BALANCE_CENTS)
    authorization_id = make_hold(card_id, HOLD_CENTS, OLD)
    assert balance(engine, card_id) == BALANCE_CENTS - HOLD_CENTS

    assert expire(engine) == 1

    assert status(engine, authorization_id) == "expired"
    assert balance(engine, card_id) == BALANCE_CENTS
    with Session(engine) as s:
        assert check_invariants(s) == []


def test_young_pending_hold_is_left_alone(
    engine: Engine, make_card: MakeCard, make_hold: MakeHold
) -> None:
    card_id = make_card(BALANCE_CENTS)
    authorization_id = make_hold(card_id, HOLD_CENTS, YOUNG)

    assert expire(engine) == 0

    assert status(engine, authorization_id) == "pending"
    assert balance(engine, card_id) == BALANCE_CENTS - HOLD_CENTS


def test_nothing_to_expire_returns_zero(engine: Engine) -> None:
    assert expire(engine) == 0


@pytest.mark.parametrize(
    ("path", "body", "final_status"),
    [
        (
            "/captures",
            {"network_capture_id": "c", "amount_cents": HOLD_CENTS},
            "captured",
        ),
        (
            "/captures",
            {"network_capture_id": "c", "amount_cents": 1_000},
            "partially_captured",
        ),
        ("/reversals", {"network_reversal_id": "r"}, "reversed"),
    ],
)
def test_old_settled_holds_are_left_alone(
    client: TestClient,
    engine: Engine,
    make_card: MakeCard,
    make_hold: MakeHold,
    path: str,
    body: dict[str, object],
    final_status: str,
) -> None:
    """Only pending holds expire: a settled one must not be released again."""
    card_id = make_card(BALANCE_CENTS)
    authorization_id = make_hold(card_id, HOLD_CENTS, OLD)
    r = client.post(path, json={**body, "authorization_id": authorization_id})
    assert r.status_code == 200
    before = balance(engine, card_id)

    assert expire(engine) == 0

    assert status(engine, authorization_id) == final_status
    assert balance(engine, card_id) == before


def test_limit_caps_one_batch(
    engine: Engine, make_card: MakeCard, make_hold: MakeHold
) -> None:
    card_id = make_card(BALANCE_CENTS)
    for _ in range(15):
        make_hold(card_id, 100, OLD)

    assert expire(engine, limit=10) == 10
    assert expire(engine, limit=10) == 5
    assert expire(engine, limit=10) == 0
    assert balance(engine, card_id) == BALANCE_CENTS


def test_oldest_holds_expire_first(
    engine: Engine, make_card: MakeCard, make_hold: MakeHold
) -> None:
    card_id = make_card(BALANCE_CENTS)
    newer = make_hold(card_id, 100, OLD)
    older = make_hold(card_id, 100, OLD + 600)

    assert expire(engine, limit=1) == 1

    assert status(engine, older) == "expired"
    assert status(engine, newer) == "pending"


def test_each_card_gets_back_exactly_its_own_holds(
    engine: Engine, make_card: MakeCard, make_hold: MakeHold
) -> None:
    """Several holds on one card are summed; other cards aren't touched."""
    card_a = make_card(BALANCE_CENTS)
    card_b = make_card(BALANCE_CENTS)
    card_c = make_card(BALANCE_CENTS)
    for amount in (1_000, 2_000, 3_000):
        make_hold(card_a, amount, OLD)
    make_hold(card_b, 4_000, OLD)
    young_on_b = make_hold(card_b, 500, YOUNG)
    make_hold(card_c, 700, YOUNG)

    assert expire(engine) == 4

    assert balance(engine, card_a) == BALANCE_CENTS
    assert balance(engine, card_b) == BALANCE_CENTS - 500
    assert status(engine, young_on_b) == "pending"
    assert balance(engine, card_c) == BALANCE_CENTS - 700
    with Session(engine) as s:
        assert check_invariants(s) == []


def test_locked_hold_is_skipped_without_waiting(
    engine: Engine, make_card: MakeCard, make_hold: MakeHold
) -> None:
    """SKIP LOCKED: a hold another transaction has locked is passed over.

    The other transaction stands in for a second worker's batch, or a capture
    in progress. Without SKIP LOCKED, expire_holds would wait for it; the
    lock_timeout turns that wait into an error instead of a hung test.
    """
    card_id = make_card(BALANCE_CENTS)
    locked = make_hold(card_id, 1_000, OLD + 600)  # the oldest: first in line
    free = make_hold(card_id, 2_000, OLD)

    with engine.connect() as other:
        # engine.connect() starts a transaction on first use; the row lock
        # lasts until it ends.
        other.execute(
            text("SELECT id FROM holds WHERE authorization_id = :a FOR UPDATE"),
            {"a": locked},
        )
        with Session(engine) as s:
            s.execute(text("SET lock_timeout = '2s'"))
            assert expire_holds(s, older_than_seconds=WINDOW_SECONDS) == 1
        assert status(engine, free) == "expired"
        assert status(engine, locked) == "pending"
        other.rollback()

    # Once the lock is gone, the next run picks it up.
    assert expire(engine) == 1
    assert status(engine, locked) == "expired"
    assert balance(engine, card_id) == BALANCE_CENTS


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/captures", {"network_capture_id": "c", "amount_cents": HOLD_CENTS}),
        ("/reversals", {"network_reversal_id": "r"}),
    ],
)
def test_settling_an_expired_hold_is_409(
    client: TestClient,
    engine: Engine,
    make_card: MakeCard,
    make_hold: MakeHold,
    path: str,
    body: dict[str, object],
) -> None:
    card_id = make_card(BALANCE_CENTS)
    authorization_id = make_hold(card_id, HOLD_CENTS, OLD)
    assert expire(engine) == 1

    r = client.post(path, json={**body, "authorization_id": authorization_id})

    assert r.status_code == 409
    assert status(engine, authorization_id) == "expired"
    assert balance(engine, card_id) == BALANCE_CENTS


def test_run_once_drains_more_than_one_batch(
    engine: Engine, make_card: MakeCard, make_hold: MakeHold
) -> None:
    card_id = make_card(BALANCE_CENTS)
    for _ in range(25):
        make_hold(card_id, 100, OLD)

    assert run_once(engine, settings(hold_expiry_batch_size=10)) == 25
    assert balance(engine, card_id) == BALANCE_CENTS


def test_run_expires_on_a_tick_and_stops_when_asked(
    engine: Engine, make_card: MakeCard, make_hold: MakeHold
) -> None:
    card_id = make_card(BALANCE_CENTS)
    authorization_id = make_hold(card_id, HOLD_CENTS, OLD)
    stop = threading.Event()
    worker = threading.Thread(target=run, args=(engine, settings(), stop))
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while status(engine, authorization_id) != "expired":
            assert time.monotonic() < deadline, "the worker never expired the hold"
            time.sleep(0.01)
    finally:
        stop.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert balance(engine, card_id) == BALANCE_CENTS


def test_run_keeps_going_after_a_failed_tick(
    engine: Engine,
    make_card: MakeCard,
    make_hold: MakeHold,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One bad run (say, the database restarting) mustn't kill the worker."""
    import authz.workers.hold_expiry as worker_module

    calls = 0
    real_run_once = worker_module.run_once

    def flaky_run_once(engine: Engine, settings: Settings) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database went away")
        return real_run_once(engine, settings)

    monkeypatch.setattr(worker_module, "run_once", flaky_run_once)
    card_id = make_card(BALANCE_CENTS)
    authorization_id = make_hold(card_id, HOLD_CENTS, OLD)
    stop = threading.Event()
    worker = threading.Thread(target=run, args=(engine, settings(), stop))
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while status(engine, authorization_id) != "expired":
            assert time.monotonic() < deadline, "the worker stopped after a failure"
            time.sleep(0.01)
    finally:
        stop.set()
        worker.join(timeout=5)
    assert calls >= 2
