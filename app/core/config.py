"""Application settings, read from environment variables (and `.env` when present).

Every value has a safe local-development default except secrets, which must come
from the environment in staging/production. See `.env.example` for the full list.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnv(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ app
    app_name: str = "kaladhaga-backend"
    app_version: str = "0.1.0"
    app_env: AppEnv = AppEnv.LOCAL

    # ------------------------------------------------------------------ logging
    log_level: str = "INFO"
    # "json" = one JSON object per line (machine readable, used in files and prod).
    # "console" = human-readable single lines for local terminals.
    log_format: Literal["json", "console"] = "console"
    log_to_file: bool = True
    log_dir: Path = Path("logs")
    # Rotated daily (UTC midnight); this many days of files are kept on disk.
    log_retention_days: int = Field(default=90, ge=1)
    # Log SQL statements (very noisy; local debugging only).
    log_sql: bool = False

    # ------------------------------------------------------------------ database / redis
    database_url: str = "postgresql+asyncpg://kaladhaga:kaladhaga@localhost:5433/kaladhaga"
    database_pool_size: int = 10
    redis_url: str = "redis://localhost:6380/0"

    # ------------------------------------------------------------------ providers
    # "fake" logs to the console and never contacts anyone.
    telephony_provider: Literal["fake", "exotel"] = "fake"
    sms_provider: Literal["fake", "exotel"] = "fake"
    push_provider: Literal["fake", "fcm"] = "fake"

    # ------------------------------------------------------------------ secrets (used from B1 on)
    jwt_secret: SecretStr = SecretStr("dev-only-change-me")

    @property
    def is_test(self) -> bool:
        return self.app_env is AppEnv.TEST

    @property
    def is_deployed(self) -> bool:
        return self.app_env in (AppEnv.STAGING, AppEnv.PRODUCTION)

    @model_validator(mode="after")
    def _check_safety(self) -> Settings:
        # Tests must never reach a real provider (CLAUDE.md rule).
        if self.is_test:
            real = {
                name: value
                for name, value in (
                    ("TELEPHONY_PROVIDER", self.telephony_provider),
                    ("SMS_PROVIDER", self.sms_provider),
                    ("PUSH_PROVIDER", self.push_provider),
                )
                if value != "fake"
            }
            if real:
                raise ValueError(f"APP_ENV=test requires fake providers, got {real}")
        if self.is_deployed and self.jwt_secret.get_secret_value() == "dev-only-change-me":
            raise ValueError("JWT_SECRET must be set in staging/production")
        return self

    def safe_summary(self) -> dict[str, object]:
        """Non-secret settings, safe to write to the startup log."""
        return {
            "app_env": self.app_env.value,
            "app_version": self.app_version,
            "log_level": self.log_level,
            "log_format": self.log_format,
            "log_to_file": self.log_to_file,
            "telephony_provider": self.telephony_provider,
            "sms_provider": self.sms_provider,
            "push_provider": self.push_provider,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
