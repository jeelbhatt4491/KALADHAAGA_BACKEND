"""GET /health: liveness + dependency checks (database, redis).

Returns 200 when every dependency answers, 503 otherwise (so a load balancer
takes the instance out). Failure details go to the log, not the response.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.core.logging import get_logger

router = APIRouter(tags=["health"])
log = get_logger("kaladhaga.health")

CHECK_TIMEOUT_S = 2.0


class CheckResult(BaseModel):
    status: Literal["ok", "error"]
    latency_ms: float


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    service: str
    version: str
    env: str
    checks: dict[str, CheckResult]


async def _timed(name: str, probe: Callable[[], Awaitable[object]]) -> CheckResult:
    started = time.perf_counter()
    try:
        await asyncio.wait_for(probe(), timeout=CHECK_TIMEOUT_S)
        status: Literal["ok", "error"] = "ok"
    except Exception as exc:
        status = "error"
        log.error("health.check_failed", check=name, error=type(exc).__name__, detail=str(exc))
    return CheckResult(status=status, latency_ms=round((time.perf_counter() - started) * 1000, 1))


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse, "description": "A dependency is down"}},
    summary="Service health",
)
async def health(request: Request, response: Response) -> HealthResponse:
    settings: Settings = request.app.state.settings
    engine: AsyncEngine = request.app.state.engine
    redis: Redis = request.app.state.redis

    async def db_probe() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def redis_probe() -> None:
        await redis.ping()

    database, redis_result = await asyncio.gather(
        _timed("database", db_probe), _timed("redis", redis_probe)
    )
    checks = {"database": database, "redis": redis_result}
    healthy = all(c.status == "ok" for c in checks.values())
    if not healthy:
        response.status_code = 503
    return HealthResponse(
        status="ok" if healthy else "degraded",
        service=settings.app_name,
        version=settings.app_version,
        env=settings.app_env.value,
        checks=checks,
    )
