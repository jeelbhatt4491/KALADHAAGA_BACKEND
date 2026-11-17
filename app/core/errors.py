"""API errors.

The app expects errors as 4xx JSON `{code, ...}` (e.g. `{code: "invalid_otp",
attempt: 2, max_attempts: 5}`). Raise `AppError` from services/endpoints; the
handlers below turn it into that shape, add `request_id` (so a user report can
be matched to the logs) and log it.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.context import request_id
from app.core.logging import get_logger

log = get_logger("kaladhaga.errors")

_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    429: "rate_limited",
}


class AppError(Exception):
    """A business/API error with a stable machine-readable `code`."""

    def __init__(self, code: str, status_code: int = 400, **extra: Any) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.extra = extra


def _body(code: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, **extra, "request_id": request_id()}


async def _app_error(_: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, AppError):  # pragma: no cover
        raise TypeError(type(exc))
    level = log.error if exc.status_code >= 500 else log.warning
    level("request.app_error", code=exc.code, status=exc.status_code, **exc.extra)
    return JSONResponse(_body(exc.code, **exc.extra), status_code=exc.status_code)


async def _validation_error(_: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):  # pragma: no cover
        raise TypeError(type(exc))
    # Never echo or log the submitted values (they may be passwords or OTPs).
    errors = [
        {"loc": list(err.get("loc", ())), "type": err.get("type"), "msg": err.get("msg")}
        for err in exc.errors()
    ]
    log.warning("request.validation_error", errors=errors)
    return JSONResponse(_body("validation_error", errors=errors), status_code=422)


async def _http_error(_: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):  # pragma: no cover
        raise TypeError(type(exc))
    code = _HTTP_CODES.get(exc.status_code, "http_error")
    if exc.status_code >= 500:
        log.error("request.http_error", status=exc.status_code, detail=exc.detail)
    return JSONResponse(
        _body(code), status_code=exc.status_code, headers=getattr(exc, "headers", None)
    )


def install_error_handlers(app: FastAPI) -> None:
    # Unexpected exceptions are caught and logged by RequestLoggingMiddleware,
    # inside the request context, and returned as 500 {code: "internal_error"}.
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
