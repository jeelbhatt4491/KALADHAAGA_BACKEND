"""FastAPI application. Routers are mounted in `create_app`.

Run locally:  docker compose up      (or: uv run uvicorn app.main:app --reload)
API docs:     http://localhost:8000/docs
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from app.api import health
from app.core.config import Settings, get_settings
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestLoggingMiddleware
from app.db.session import create_engine, create_session_factory
from app.providers import build_providers

log = get_logger("kaladhaga.app")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings, service="api")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info("app.starting", **settings.safe_summary())
        engine = create_engine(settings)
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        app.state.redis = redis
        app.state.providers = build_providers(settings)
        log.info("app.started")
        try:
            yield
        finally:
            log.info("app.stopping")
            await redis.aclose()
            await engine.dispose()
            log.info("app.stopped")

    app = FastAPI(
        title="Kala Dhaga API",
        version=settings.app_version,
        description=(
            "Backend for the Kala Dhaga safety band: account API for the app, "
            "band API (`/device/v1`), webhooks and the live map page."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(RequestLoggingMiddleware)
    install_error_handlers(app)
    app.include_router(health.router)
    return app


app = create_app()
