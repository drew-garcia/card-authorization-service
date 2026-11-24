"""Configuration, read from environment variables (and a local .env file)."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Values come from the environment first, then from .env. Field names
    # match case-insensitively, so DATABASE_URL fills database_url.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # No default on purpose: a missing URL should fail loudly at startup.
    database_url: str

    # BENCH ONLY: which race fix authorize() uses.
    authz_race_strategy: Literal["lock", "conditional", "serializable"] = "lock"


@lru_cache
def get_settings() -> Settings:
    """Build the settings once and reuse them (lru_cache memoises the call)."""
    return Settings()  # pyright: ignore[reportCallIssue]  # filled from env
