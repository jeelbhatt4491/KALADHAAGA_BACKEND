"""Per-request / per-job log context.

Anything bound here (request_id, user_id, alert_id, device_id, job_id, ...) is
attached automatically to every log line written while handling that request or
job, so one alert can be traced end to end across the api and the worker.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_log_context: ContextVar[dict[str, Any] | None] = ContextVar("log_context", default=None)


def get_log_context() -> dict[str, Any]:
    return dict(_log_context.get() or {})


def bind_log_context(**values: Any) -> None:
    """Add keys to the current context (e.g. after auth: bind_log_context(user_id=...))."""
    current = get_log_context()
    current.update({k: v for k, v in values.items() if v is not None})
    _log_context.set(current)


@contextmanager
def log_context(**values: Any) -> Iterator[None]:
    """Bind keys for the duration of a block, then restore the previous context."""
    token = _log_context.set({**get_log_context(), **values})
    try:
        yield
    finally:
        _log_context.reset(token)


def request_id() -> str | None:
    value = get_log_context().get("request_id")
    return str(value) if value is not None else None
