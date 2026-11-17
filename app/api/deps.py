"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client import ClientInfo
from app.core.config import Settings
from app.core.context import bind_log_context
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.security import InvalidAccessTokenError, decode_access_token
from app.db.models import User
from app.db.session import get_session
from app.providers import Providers

log = get_logger("kaladhaga.auth")
_bearer = HTTPBearer(auto_error=False, description="Access token from /auth/login")


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _redis(request: Request) -> Redis:
    redis: Redis = request.app.state.redis
    return redis


def _providers(request: Request) -> Providers:
    providers: Providers = request.app.state.providers
    return providers


def _client(request: Request) -> ClientInfo:
    ua = request.headers.get("user-agent")
    return ClientInfo(
        ip=getattr(request.state, "client_ip", None),
        user_agent=ua[:255] if ua else None,
    )


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(_settings)]
RedisDep = Annotated[Redis, Depends(_redis)]
ProvidersDep = Annotated[Providers, Depends(_providers)]
ClientDep = Annotated[ClientInfo, Depends(_client)]


async def _current_user(
    session: SessionDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    """Resolves the Bearer access token. Any problem is a plain 401, which makes
    the app refresh its token once and retry."""
    if credentials is None:
        raise AppError("unauthorized", 401)
    try:
        user_id = decode_access_token(settings, credentials.credentials)
    except InvalidAccessTokenError as exc:
        log.info("auth.access_token_rejected", reason=exc.reason)
        raise AppError("unauthorized", 401) from exc
    user = await session.get(User, user_id)
    if user is None:
        log.warning("auth.access_token_unknown_user", user_id=str(user_id))
        raise AppError("unauthorized", 401)
    bind_log_context(user_id=str(user.id))
    return user


CurrentUser = Annotated[User, Depends(_current_user)]


PhotoUrl = Callable[[str | None], str | None]


def _photo_url(request: Request, settings: SettingsDep, providers: ProvidersDep) -> PhotoUrl:
    """Turns a stored photo key into a short-lived signed link (null stays null)."""
    base = (settings.public_base_url or str(request.base_url)).rstrip("/")

    def url(key: str | None) -> str | None:
        return f"{base}{providers.storage.signed_path(key)}" if key else None

    return url


PhotoUrlDep = Annotated[PhotoUrl, Depends(_photo_url)]
