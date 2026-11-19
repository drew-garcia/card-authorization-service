from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from authz.api.deps import get_store
from authz.main import app
from authz.store import CardStore


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A TestClient backed by a fresh, empty store, so tests don't share cards."""
    store = CardStore()
    app.dependency_overrides[get_store] = lambda: store
    yield TestClient(app)
    app.dependency_overrides.clear()
