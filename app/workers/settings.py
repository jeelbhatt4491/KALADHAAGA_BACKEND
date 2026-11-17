"""arq worker entry point:  arq app.workers.settings.WorkerSettings"""

from __future__ import annotations

from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import create_engine, create_session_factory
from app.providers import build_providers
from app.workers.tasks import ping, worker_heartbeat

settings = get_settings()
configure_logging(settings, service="worker")
log = get_logger("kaladhaga.worker")


async def startup(ctx: dict[str, Any]) -> None:
    # The arq CLI installs its own handlers after import; take logging back.
    configure_logging(settings, service="worker")
    log.info("worker.starting", **settings.safe_summary())
    engine = create_engine(settings)
    ctx["engine"] = engine
    ctx["session_factory"] = create_session_factory(engine)
    ctx["providers"] = build_providers(settings)
    log.info("worker.started")


async def shutdown(ctx: dict[str, Any]) -> None:
    log.info("worker.stopping")
    await ctx["engine"].dispose()
    log.info("worker.stopped")


class WorkerSettings:
    functions: ClassVar[list[Any]] = [ping, worker_heartbeat]
    cron_jobs: ClassVar[list[Any]] = [
        cron(worker_heartbeat, second=0, run_at_startup=True)  # type: ignore[arg-type]
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    # Keep job results for a day for debugging.
    keep_result = 86_400
