"""Application configuration. Everything comes from the environment."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCOREBOARD_", extra="ignore")

    env: str = "dev"
    secret_key: str = "dev-only-not-for-production"
    encryption_key: str = ""

    # Read without the SCOREBOARD_ prefix; it is a well-known name.
    database_url: str = "postgresql+psycopg://scoreboard:scoreboard@db:5432/scoreboard_dev"

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"


@lru_cache
def settings() -> Settings:
    import os

    values = {}
    if os.environ.get("DATABASE_URL"):
        values["database_url"] = os.environ["DATABASE_URL"]
    return Settings(**values)
