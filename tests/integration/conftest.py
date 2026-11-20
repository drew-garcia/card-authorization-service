"""Fixtures for integration tests. The shared database fixtures are in tests/conftest.py."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlmodel import Session

from authz.db import get_session
from authz.main import app


@pytest.fixture(autouse=True)
def _clean_tables_before_each_test(clean_tables: None) -> None:
    """autouse=True applies this to every test in this folder, even ones that
    don't ask for it, so each one starts with empty tables."""


@pytest.fixture
def client(engine: Engine) -> Iterator[TestClient]:
    """A TestClient whose requests each get their own session on authz_test."""

    def test_session() -> Iterator[Session]:
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = test_session
    yield TestClient(app)
    app.dependency_overrides.clear()
