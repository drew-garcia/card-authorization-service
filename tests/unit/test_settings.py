"""Hold expiry settings: sensible defaults, and nonsense is refused at startup."""

import pytest
from pydantic import ValidationError

from authz.settings import Settings

DUMMY_URL = "postgresql+psycopg://user:pass@localhost:1/none"


@pytest.fixture(autouse=True)
def _no_expiry_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ignore any HOLD_EXPIRY_* set in the shell, so the defaults are tested."""
    for name in (
        "HOLD_EXPIRY_SECONDS",
        "HOLD_EXPIRY_INTERVAL_SECONDS",
        "HOLD_EXPIRY_BATCH_SIZE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_hold_expiry_defaults() -> None:
    # _env_file=None: don't read a developer's .env either.
    s = Settings(database_url=DUMMY_URL, _env_file=None)  # pyright: ignore[reportCallIssue]
    assert s.hold_expiry_seconds == 7 * 24 * 60 * 60
    assert s.hold_expiry_interval_seconds == 60
    assert s.hold_expiry_batch_size == 100


def test_hold_expiry_window_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOLD_EXPIRY_SECONDS", "86400")
    s = Settings(database_url=DUMMY_URL, _env_file=None)  # pyright: ignore[reportCallIssue]
    assert s.hold_expiry_seconds == 86_400


@pytest.mark.parametrize(
    "name",
    ["HOLD_EXPIRY_SECONDS", "HOLD_EXPIRY_INTERVAL_SECONDS", "HOLD_EXPIRY_BATCH_SIZE"],
)
@pytest.mark.parametrize("value", ["0", "-1"])
def test_hold_expiry_settings_must_be_positive(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    """A zero window would expire every hold the moment it was placed."""
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings(database_url=DUMMY_URL, _env_file=None)  # pyright: ignore[reportCallIssue]
