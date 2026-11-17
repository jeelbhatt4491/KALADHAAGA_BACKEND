"""Logging for the api and the worker.

Design (see README "Logging"):
- Every line is structured: an event name plus key/value fields, never free-form
  prose with values glued in. Example::

      log = get_logger(__name__)
      log.info("otp.sent", phone=phone, purpose="signup")

- Every record passes through `MaskingFilter` (phones, OTPs, tokens, secrets,
  exact coordinates are masked) and `ContextFilter` (request_id, user_id,
  alert_id, job_id, ... from `app.core.context`).
- Outputs:
    stdout                         console or JSON (LOG_FORMAT)
    logs/<service>.log             JSON, everything at LOG_LEVEL and above
    logs/<service>.error.log       JSON, WARNING and above (quick triage)
    logs/audit-<service>.log       JSON, only the audit trail (`kaladhaga.audit`)
  Files rotate daily at UTC midnight and keep LOG_RETENTION_DAYS days.
- All timestamps are UTC, ISO 8601 with milliseconds.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import traceback
from datetime import UTC, datetime
from logging.handlers import TimedRotatingFileHandler
from types import TracebackType
from typing import Any, ClassVar

from app.core.config import Settings
from app.core.context import get_log_context
from app.core.masking import mask_mapping, mask_text

AUDIT_LOGGER = "kaladhaga.audit"
_HANDLER_TAG = "_kaladhaga_handler"

# Attributes every LogRecord has; anything else on a record is an "extra".
_STANDARD_ATTRS = set(
    logging.LogRecord("x", logging.INFO, "x", 0, "x", None, None).__dict__.keys()
) | {"message", "asctime", "taskName"}


def _utc_iso(ts: float) -> str:
    return (
        datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _record_fields(record: logging.LogRecord) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, value in record.__dict__.items():
        if (
            key in _STANDARD_ATTRS
            or key.startswith("_")
            or key in ("fields", "event", "ctx", "color_message")
        ):
            continue
        fields[key] = value
    explicit = getattr(record, "fields", None)
    if isinstance(explicit, dict):
        fields.update(explicit)
    return fields


# ---------------------------------------------------------------------------- filters


class ContextFilter(logging.Filter):
    """Copies the current request/job context onto the record."""

    def __init__(self, service: str, env: str, version: str) -> None:
        super().__init__()
        self.service = service
        self.env = env
        self.version = version

    def filter(self, record: logging.LogRecord) -> bool:
        # A record visits every handler; bind (and later mask) the context only once.
        if hasattr(record, "ctx"):
            return True
        record.ctx = get_log_context()
        record._service = self.service
        record._env = self.env
        record._version = self.version
        return True


class MaskingFilter(logging.Filter):
    """Masks secrets and personal data in the message, the fields and the context."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "_masked", False):
            return True
        try:
            message = record.getMessage()
        except Exception:  # bad %-format args: keep the raw template
            message = str(record.msg)
        record.msg = mask_text(message)
        record.args = None
        fields = _record_fields(record)
        for key in list(record.__dict__):
            if key not in _STANDARD_ATTRS and not key.startswith("_") and key != "ctx":
                delattr(record, key)
        record.fields = mask_mapping(fields)
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict):
            record.ctx = mask_mapping(ctx)
        record._masked = True
        return True


class _OnlyLogger(logging.Filter):
    def __init__(self, name: str, include: bool) -> None:
        super().__init__()
        self.prefix = name
        self.include = include

    def filter(self, record: logging.LogRecord) -> bool:
        matches = record.name == self.prefix or record.name.startswith(self.prefix + ".")
        return matches if self.include else not matches


# ---------------------------------------------------------------------------- formatters


def _exception_text(record: logging.LogRecord) -> str | None:
    if not record.exc_info or record.exc_info[0] is None:
        return None
    return mask_text("".join(traceback.format_exception(*record.exc_info)).rstrip())


class JsonFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__()
        self.host = socket.gethostname()
        self.pid = os.getpid()

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _utc_iso(record.created),
            "level": record.levelname,
            "service": getattr(record, "_service", None),
            "env": getattr(record, "_env", None),
            "version": getattr(record, "_version", None),
            "logger": record.name,
            "event": record.getMessage(),
        }
        ctx = getattr(record, "ctx", None)
        if ctx:
            payload.update(ctx)
        fields = getattr(record, "fields", None)
        if fields:
            payload["data"] = fields
        payload["src"] = f"{record.module}:{record.funcName}:{record.lineno}"
        payload["host"] = self.host
        payload["pid"] = self.pid
        exc = _exception_text(record)
        if exc:
            exc_cls = record.exc_info[0] if record.exc_info else None
            payload["exc_type"] = exc_cls.__name__ if exc_cls else None
            payload["exc"] = exc
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    _COLORS: ClassVar[dict[str, str]] = {
        "DEBUG": "\x1b[90m",
        "INFO": "\x1b[32m",
        "WARNING": "\x1b[33m",
        "ERROR": "\x1b[31m",
        "CRITICAL": "\x1b[41m",
    }
    _RESET = "\x1b[0m"

    def __init__(self, color: bool) -> None:
        super().__init__()
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        level = record.levelname
        if self.color:
            level = f"{self._COLORS.get(record.levelname, '')}{level:<8}{self._RESET}"
        else:
            level = f"{level:<8}"
        parts = [_utc_iso(record.created), level, f"[{record.name}]", record.getMessage()]
        extras = {**(getattr(record, "ctx", None) or {}), **(getattr(record, "fields", None) or {})}
        if extras:
            parts.append(" ".join(f"{k}={json.dumps(v, default=str)}" for k, v in extras.items()))
        line = " ".join(parts)
        exc = _exception_text(record)
        return f"{line}\n{exc}" if exc else line


# ---------------------------------------------------------------------------- event logger


class EventLogger:
    """Thin wrapper so call sites read `log.info("alert.created", alert_id=...)`."""

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    @property
    def name(self) -> str:
        return self._logger.name

    def _log(self, level: int, event: str, fields: dict[str, Any], exc_info: bool) -> None:
        if self._logger.isEnabledFor(level):
            self._logger.log(
                level, event, extra={"fields": fields}, exc_info=exc_info, stacklevel=3
            )

    def debug(self, event: str, /, **fields: Any) -> None:
        self._log(logging.DEBUG, event, fields, False)

    def info(self, event: str, /, **fields: Any) -> None:
        self._log(logging.INFO, event, fields, False)

    def warning(self, event: str, /, **fields: Any) -> None:
        self._log(logging.WARNING, event, fields, False)

    def error(self, event: str, /, exc_info: bool = False, **fields: Any) -> None:
        self._log(logging.ERROR, event, fields, exc_info)

    def critical(self, event: str, /, exc_info: bool = False, **fields: Any) -> None:
        self._log(logging.CRITICAL, event, fields, exc_info)

    def exception(self, event: str, /, **fields: Any) -> None:
        """ERROR with the current exception's traceback attached."""
        self._log(logging.ERROR, event, fields, True)


def get_logger(name: str) -> EventLogger:
    return EventLogger(name)


# ---------------------------------------------------------------------------- setup


def _file_handler(path: str, retention_days: int) -> TimedRotatingFileHandler:
    handler = TimedRotatingFileHandler(
        path,
        when="midnight",
        utc=True,
        backupCount=retention_days,
        encoding="utf-8",
        delay=True,
    )
    handler.suffix = "%Y-%m-%d"
    return handler


def configure_logging(settings: Settings, service: str) -> None:
    """Install handlers on the root logger. Safe to call more than once."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_TAG, False):
            root.removeHandler(handler)
            handler.close()

    level = logging.getLevelName(settings.log_level.upper())
    if not isinstance(level, int):
        level = logging.INFO
    root.setLevel(level)

    context_filter = ContextFilter(service, settings.app_env.value, settings.app_version)
    masking_filter = MaskingFilter()
    json_formatter = JsonFormatter()

    handlers: list[logging.Handler] = []

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(
        json_formatter
        if settings.log_format == "json"
        else ConsoleFormatter(color=sys.stdout.isatty())
    )
    handlers.append(stream)

    if settings.log_to_file:
        log_dir = settings.log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        days = settings.log_retention_days

        main_file = _file_handler(str(log_dir / f"{service}.log"), days)
        main_file.addFilter(_OnlyLogger(AUDIT_LOGGER, include=False))
        handlers.append(main_file)

        error_file = _file_handler(str(log_dir / f"{service}.error.log"), days)
        error_file.setLevel(logging.WARNING)
        handlers.append(error_file)

        audit_file = _file_handler(str(log_dir / f"audit-{service}.log"), days)
        audit_file.addFilter(_OnlyLogger(AUDIT_LOGGER, include=True))
        handlers.append(audit_file)

        for handler in (main_file, error_file, audit_file):
            handler.setFormatter(json_formatter)

    for handler in handlers:
        handler.addFilter(context_filter)
        handler.addFilter(masking_filter)
        setattr(handler, _HANDLER_TAG, True)
        root.addHandler(handler)

    # Third-party loggers: send everything through our handlers.
    for name in ("uvicorn", "uvicorn.error", "arq", "arq.worker", "arq.jobs", "alembic"):
        lib_logger = logging.getLogger(name)
        lib_logger.handlers.clear()
        lib_logger.propagate = True
    # Our middleware writes one structured line per request instead.
    logging.getLogger("uvicorn.access").handlers.clear()
    logging.getLogger("uvicorn.access").propagate = False
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.log_sql else logging.WARNING
    )
    for noisy in ("asyncio", "watchfiles", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.captureWarnings(True)
    _install_excepthook()


def _install_excepthook() -> None:
    crash_log = logging.getLogger("kaladhaga.crash")

    def hook(
        exc_type: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
    ) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        crash_log.critical("process.uncaught_exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = hook
