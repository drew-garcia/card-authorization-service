from uuid import uuid4

import pytest

from authz.store import CardStore


def test_create_then_get_round_trips() -> None:
    store = CardStore()
    card = store.create(initial_balance_cents=500)
    assert card.available_cents == 500
    assert store.get(card.id) == card


def test_create_gives_distinct_ids() -> None:
    store = CardStore()
    assert store.create(0).id != store.create(0).id


def test_get_missing_returns_none() -> None:
    assert CardStore().get(uuid4()) is None


def test_set_available_updates_balance() -> None:
    store = CardStore()
    card = store.create(500)
    store.set_available(card.id, 200)
    found = store.get(card.id)
    assert found is not None
    assert found.available_cents == 200


def test_set_available_on_missing_card_raises() -> None:
    with pytest.raises(KeyError):
        CardStore().set_available(uuid4(), 100)
