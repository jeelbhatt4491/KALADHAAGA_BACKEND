"""Request logging middleware (pure ASGI, so it also covers WebSockets later).

For every HTTP request:
- picks up a valid incoming `X-Request-ID` or creates one, binds it to the log
  context and returns it in the response header;
- writes exactly one `http.request` line: method, path (map tokens masked, no
  query string), status, duration, client ip, user agent. 5xx -> ERROR,
  4xx -> WARNING, otherwise INFO; `/health` hits are DEBUG to keep logs readable.
"""

from __future__ import annotations

import re
import time
import uuid

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.context import log_context
from app.core.logging import get_logger
from app.core.masking import mask_text

log = get_logger("kaladhaga.http")

REQUEST_ID_HEADER = "x-request-id"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._\-]{8,64}$")
_QUIET_PATHS = {"/health"}


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return bytes(value).decode("latin-1")
    return None


def client_ip_from_scope(scope: Scope, trust_forwarded_for: bool) -> str | None:
    # Only behind our own load balancer is X-Forwarded-For trustworthy; otherwise
    # anyone could write any IP into the logs and the audit trail.
    forwarded = _header(scope, b"x-forwarded-for") if trust_forwarded_for else None
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else None


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp, trust_forwarded_for: bool = False) -> None:
        self.app = app
        self.trust_forwarded_for = trust_forwarded_for

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        incoming = _header(scope, REQUEST_ID_HEADER.encode())
        rid = incoming if incoming and _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        path = mask_text(scope.get("path", ""))
        method = scope.get("method", "WS")
        client_ip = client_ip_from_scope(scope, self.trust_forwarded_for)
        # Exposed to endpoints as request.state.client_ip (audit rows).
        scope.setdefault("state", {})["client_ip"] = client_ip
        status_holder: dict[str, int] = {}
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER.encode(), rid.encode()))
                message["headers"] = headers
            await send(message)

        with log_context(request_id=rid):
            try:
                await self.app(scope, receive, send_wrapper)
            except Exception as exc:
                # Handled here (not by Starlette's outer handler) so the traceback is
                # logged with the request_id and the client still gets it back.
                if "status" in status_holder or scope["type"] != "http":
                    log.exception("request.unhandled_exception", exc_type=type(exc).__name__)
                    raise
                log.exception("request.unhandled_exception", exc_type=type(exc).__name__)
                response = JSONResponse(
                    {"code": "internal_error", "request_id": rid}, status_code=500
                )
                await response(scope, receive, send_wrapper)
            finally:
                status = status_holder.get("status", 500)
                fields = {
                    "method": method,
                    "path": path,
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    "client_ip": client_ip,
                    "user_agent": _header(scope, b"user-agent"),
                }
                if status >= 500:
                    log.error("http.request", **fields)
                elif status >= 400:
                    log.warning("http.request", **fields)
                elif path in _QUIET_PATHS:
                    log.debug("http.request", **fields)
                else:
                    log.info("http.request", **fields)
