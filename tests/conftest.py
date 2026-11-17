"""Test setup.

Tests run against a separate database (`kaladhaga_test`, from TEST_DATABASE_URL)
that is wiped and migrated from scratch once per test session, and Redis db 15.
Providers are always fake (enforced by Settings when APP_ENV=test).

Needs postgres + redis running:  docker compose up -d postgres redis
Then: uv run pytest   (or inside docker: docker compose run --rm api pytest)
"""

from __future__ import annotations

import asyncio
import os
import secrets
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

# Must be set before anything imports app settings.
_TEST_DB = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://kaladhaga:kaladhaga@localhost:5433/kaladhaga_test",
)
_TEST_REDIS = os.environ.get("TEST_REDIS_URL", "redis://localhost:6380/15")
if "REDIS_URL" in os.environ and "TEST_REDIS_URL" not in os.environ:
    # Inside docker compose: same server, separate db index.
    _TEST_REDIS = os.environ["REDIS_URL"].rsplit("/", 1)[0] + "/15"
os.environ.update(
    {
        "APP_ENV": "test",
        "DATABASE_URL": _TEST_DB,
        "REDIS_URL": _TEST_REDIS,
        "LOG_TO_FILE": "false",
        "LOG_LEVEL": "DEBUG",
        "TELEPHONY_PROVIDER": "fake",
        "SMS_PROVIDER": "fake",
        "PUSH_PROVIDER": "fake",
        "STORAGE_PROVIDER": "fake",
        # Member photos written by tests go to a throwaway folder.
        "MEDIA_DIR": tempfile.mkdtemp(prefix="kaladhaga-media-"),
    }
)
# Tests exercise real random codes (read back from the fake SMS provider).
os.environ.pop("OTP_FIXED_CODE", None)

import asyncpg  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _asyncpg_dsn(url: str, database: str | None = None) -> str:
    parsed = make_url(url).set(drivername="postgresql")
    if database:
        parsed = parsed.set(database=database)
    return parsed.render_as_string(hide_password=False)


async def _reset_database(url: str) -> None:
    db_name = make_url(url).database
    assert db_name and db_name.endswith("_test"), f"refusing to wipe non-test database {db_name!r}"
    admin = await asyncpg.connect(_asyncpg_dsn(url, "postgres"))
    try:
        exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db_name)
        if not exists:
            await admin.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await admin.close()
    conn = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    finally:
        await conn.close()


@pytest.fixture(scope="session", autouse=True)
def _migrated_database() -> Iterator[None]:
    """Fresh schema built by the real migrations (so migrations are tested too)."""
    asyncio.run(_reset_database(_TEST_DB))
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", _TEST_DB)
    cfg.attributes["skip_logging_setup"] = True
    command.upgrade(cfg, "head")
    yield


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        # Rate-limit counters must not leak from one test into the next.
        await application.state.redis.flushdb()
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
async def session(app: FastAPI) -> AsyncIterator[AsyncSession]:
    async with app.state.session_factory() as db_session:
        yield db_session


def new_phone() -> str:
    """A fresh, valid Indian mobile in E.164 (the database is shared within a run)."""
    return f"+9198{secrets.randbelow(10**8):08d}"


@pytest.fixture
def phone() -> str:
    return new_phone()
