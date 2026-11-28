"""Configuration, read from environment variables (and a local .env file)."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Values come from the environment first, then from .env. Field names
    # match case-insensitively, so DATABASE_URL fills database_url.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # No default on purpose: a missing URL should fail loudly at startup.
    database_url: str

    # Database connections per process (each uvicorn worker is one). The pool
    # is fixed-size: max_overflow=0 means a burst of requests waits for a free
    # connection instead of opening extra ones. Overflow connections are
    # closed as soon as they're returned and reopened for the next request,
    # and under load that churn pushed past Postgres's max_connections (100 by
    # default), which turned the burst into 500s. Keep
    # workers * db_pool_size comfortably below max_connections.
    db_pool_size: int = 10
    db_max_overflow: int = 0

    # Hold expiry (see workers/hold_expiry.py). A pending hold older than
    # hold_expiry_seconds is expired and its cents go back to the card. 7 days
    # is a common network default for how long an authorization stays valid.
    # Field(gt=0) makes pydantic reject zero or negative values at startup:
    # a zero window would expire every hold the moment it was placed.
    hold_expiry_seconds: int = Field(default=7 * 24 * 60 * 60, gt=0)
    # How long the worker sleeps between runs.
    hold_expiry_interval_seconds: float = Field(default=60, gt=0)
    # Holds claimed (and locked) per transaction. Small batches keep each
    # transaction, and the card rows it locks, short.
    hold_expiry_batch_size: int = Field(default=100, gt=0)


@lru_cache
def get_settings() -> Settings:
    """Build the settings once and reuse them (lru_cache memoises the call)."""
    return Settings()  # pyright: ignore[reportCallIssue]  # filled from env
