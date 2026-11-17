"""arq tasks. Each task is wrapped with `logged_task`, which binds job_id/task to
the log context and logs start, success (with duration) and failure (with
traceback) so every background action leaves a trace."""

from __future__ import annotations

import functools
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.context import log_context
from app.core.logging import get_logger

log = get_logger("kaladhaga.worker")


def logged_task[R](
    func: Callable[..., Awaitable[R]],
) -> Callable[..., Awaitable[R]]:
    @functools.wraps(func)
    async def wrapper(ctx: dict[str, Any], *args: Any, **kwargs: Any) -> R:
        job_id = ctx.get("job_id")
        with log_context(job_id=job_id, task=func.__name__, job_try=ctx.get("job_try")):
            started = time.perf_counter()
            log.info("job.started")
            try:
                result = await func(ctx, *args, **kwargs)
            except Exception:
                log.exception(
                    "job.failed", duration_ms=round((time.perf_counter() - started) * 1000, 1)
                )
                raise
            log.info("job.succeeded", duration_ms=round((time.perf_counter() - started) * 1000, 1))
            return result

    return wrapper


@logged_task
async def ping(ctx: dict[str, Any]) -> str:
    """Smoke-test task: `await pool.enqueue_job("ping")`."""
    return "pong"


@logged_task
async def worker_heartbeat(ctx: dict[str, Any]) -> None:
    """Runs every minute so the logs prove the worker was alive at any moment."""
    await ctx["redis"].set("worker:heartbeat", str(int(time.time())), ex=180)
