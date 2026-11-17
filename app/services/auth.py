"""Sign-up, login, token refresh, logout and password reset.

Tokens: a short-lived JWT access token plus an opaque refresh token that is
rotated on every use. Refresh tokens of one login form a family; presenting an
already-rotated token (a sign it was stolen) revokes the whole family.
Every outcome that matters as evidence is written to the audit trail.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client import ClientInfo
from app.core.config import Settings
from app.core.context import bind_log_context
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.rate_limit import counter_value, hit_counter, phone_key, seconds_left
from app.core.security import (
    create_access_token,
    hash_password,
    hmac_hash,
    new_opaque_token,
    password_needs_rehash,
    verify_password,
)
from app.db.models import PasswordResetToken, PushToken, RefreshToken, User
from app.services.audit import record_audit
from app.services.otp import verify_otp

log = get_logger("kaladhaga.auth")


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str


def _now() -> datetime:
    return datetime.now(UTC)


def _token_hash(settings: Settings, kind: str, token: str) -> str:
    return hmac_hash(settings, f"{kind}:{token}")


async def _user_by_phone(session: AsyncSession, phone: str) -> User | None:
    user: User | None = await session.scalar(select(User).where(User.phone == phone))
    return user


def _issue_tokens(
    session: AsyncSession,
    settings: Settings,
    user: User,
    client: ClientInfo,
    family_id: uuid.UUID | None = None,
) -> tuple[TokenPair, RefreshToken]:
    raw = new_opaque_token()
    row = RefreshToken(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=_token_hash(settings, "refresh", raw),
        family_id=family_id or uuid.uuid4(),
        expires_at=_now() + timedelta(days=settings.refresh_token_ttl_days),
        user_agent=client.user_agent,
        ip=client.ip,
    )
    session.add(row)
    pair = TokenPair(access_token=create_access_token(settings, user.id), refresh_token=raw)
    return pair, row


async def _revoke_all(session: AsyncSession, user_id: uuid.UUID, reason: str) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now(), revoked_reason=reason)
    )


# ---------------------------------------------------------------------------- otp gate


async def check_otp_allowed(session: AsyncSession, *, phone: str, purpose: str) -> None:
    """Sign-up codes only for new numbers, reset codes only for existing ones."""
    exists = await _user_by_phone(session, phone) is not None
    if purpose == "signup" and exists:
        log.info("auth.otp_refused", phone=phone, purpose=purpose, reason="account_exists")
        raise AppError("account_exists", 409)
    if purpose == "reset_password" and not exists:
        log.info("auth.otp_refused", phone=phone, purpose=purpose, reason="account_not_found")
        raise AppError("account_not_found", 404)


# ---------------------------------------------------------------------------- sign-up


async def signup(
    session: AsyncSession,
    settings: Settings,
    client: ClientInfo,
    *,
    phone: str,
    password: str,
    code: str,
) -> TokenPair:
    if await _user_by_phone(session, phone) is not None:
        raise AppError("account_exists", 409)
    await verify_otp(session, settings, phone=phone, purpose="signup", code=code)

    user = User(
        id=uuid.uuid4(),
        phone=phone,
        password_hash=hash_password(password),
        phone_verified_at=_now(),
    )
    session.add(user)
    try:
        await session.flush()  # the user row must exist before its refresh token
    except IntegrityError as exc:
        await session.rollback()
        if "uq_users_phone" in str(exc.orig):  # same number signed up at the same moment
            log.warning("auth.signup_race", phone=phone)
            raise AppError("account_exists", 409) from exc
        raise
    pair, _ = _issue_tokens(session, settings, user, client)
    record_audit(
        session,
        action="auth.signup",
        actor_type="user",
        actor_id=str(user.id),
        entity_type="user",
        entity_id=str(user.id),
        ip=client.ip,
        details={"phone": phone, "user_agent": client.user_agent},
    )
    await session.commit()
    bind_log_context(user_id=str(user.id))
    log.info("auth.signup_succeeded", phone=phone)
    return pair


# ---------------------------------------------------------------------------- login


async def login(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    client: ClientInfo,
    *,
    phone: str,
    password: str,
) -> TokenPair:
    fail_key = phone_key("login:failures", phone)
    if await counter_value(redis, fail_key) >= settings.login_max_failures:
        retry = await seconds_left(redis, fail_key)
        log.warning("auth.login_blocked", phone=phone, retry_after=retry)
        raise AppError("too_many_attempts", 429, retry_after=retry)

    user = await _user_by_phone(session, phone)
    password_ok = verify_password(user.password_hash if user else None, password)
    if user is None or not password_ok:
        reason = "account_not_found" if user is None else "wrong_password"
        failures = await hit_counter(redis, fail_key, settings.login_lock_s)
        locked = failures >= settings.login_max_failures
        record_audit(
            session,
            action="auth.login_failed",
            actor_type="user",
            actor_id=str(user.id) if user else None,
            entity_type="user",
            entity_id=str(user.id) if user else None,
            ip=client.ip,
            details={"phone": phone, "reason": reason, "failures": failures, "locked": locked},
        )
        await session.commit()
        log.warning("auth.login_failed", phone=phone, reason=reason, failures=failures)
        if locked:
            raise AppError("too_many_attempts", 429, retry_after=settings.login_lock_s)
        if user is None:
            raise AppError("account_not_found", 404)
        raise AppError("invalid_credentials", 401)

    await redis.delete(fail_key)
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    pair, row = _issue_tokens(session, settings, user, client)
    record_audit(
        session,
        action="auth.login",
        actor_type="user",
        actor_id=str(user.id),
        entity_type="user",
        entity_id=str(user.id),
        ip=client.ip,
        details={"user_agent": client.user_agent, "login_family": str(row.family_id)},
    )
    await session.commit()
    bind_log_context(user_id=str(user.id))
    log.info("auth.login_succeeded")
    return pair


# ---------------------------------------------------------------------------- refresh / logout


async def refresh(
    session: AsyncSession, settings: Settings, client: ClientInfo, *, refresh_token: str
) -> TokenPair:
    row = await session.scalar(
        select(RefreshToken)
        .where(RefreshToken.token_hash == _token_hash(settings, "refresh", refresh_token))
        .with_for_update()
    )
    if row is None:
        log.warning("auth.refresh_rejected", reason="unknown_token")
        raise AppError("invalid_refresh_token", 401)
    bind_log_context(user_id=str(row.user_id))
    if row.revoked_at is not None:
        if row.revoked_reason == "rotated":
            # A rotated token came back: someone else has a copy. End that login everywhere.
            await session.execute(
                update(RefreshToken)
                .where(RefreshToken.family_id == row.family_id, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=_now(), revoked_reason="reuse_detected")
            )
            record_audit(
                session,
                action="auth.refresh_reuse_detected",
                actor_type="user",
                actor_id=str(row.user_id),
                entity_type="user",
                entity_id=str(row.user_id),
                ip=client.ip,
                details={"login_family": str(row.family_id), "user_agent": client.user_agent},
            )
            await session.commit()
            log.error("auth.refresh_reuse_detected", login_family=str(row.family_id))
        else:
            log.warning("auth.refresh_rejected", reason=f"revoked:{row.revoked_reason}")
        raise AppError("invalid_refresh_token", 401)
    if row.expires_at <= _now():
        log.info("auth.refresh_rejected", reason="expired")
        raise AppError("invalid_refresh_token", 401)
    user = await session.get(User, row.user_id)
    if user is None:  # pragma: no cover - cascade deletes tokens with the user
        raise AppError("invalid_refresh_token", 401)

    pair, new_row = _issue_tokens(session, settings, user, client, family_id=row.family_id)
    row.revoked_at = _now()
    row.revoked_reason = "rotated"
    row.replaced_by_id = new_row.id
    await session.commit()
    log.info("auth.token_refreshed", login_family=str(row.family_id))
    return pair


async def logout(
    session: AsyncSession,
    settings: Settings,
    client: ClientInfo,
    *,
    refresh_token: str,
    push_token: str | None,
) -> None:
    """Idempotent: an unknown or already revoked token is not an error."""
    row = await session.scalar(
        select(RefreshToken).where(
            RefreshToken.token_hash == _token_hash(settings, "refresh", refresh_token)
        )
    )
    if row is None:
        log.info("auth.logout_ignored", reason="unknown_token")
        return
    bind_log_context(user_id=str(row.user_id))
    if row.revoked_at is None:
        row.revoked_at = _now()
        row.revoked_reason = "logout"
    if push_token:
        await session.execute(
            delete(PushToken).where(PushToken.token == push_token, PushToken.user_id == row.user_id)
        )
    record_audit(
        session,
        action="auth.logout",
        actor_type="user",
        actor_id=str(row.user_id),
        entity_type="user",
        entity_id=str(row.user_id),
        ip=client.ip,
        details={"login_family": str(row.family_id), "push_unregistered": bool(push_token)},
    )
    await session.commit()
    log.info("auth.logout")


# ---------------------------------------------------------------------------- password reset


async def verify_reset_code(
    session: AsyncSession, settings: Settings, client: ClientInfo, *, phone: str, code: str
) -> str:
    user = await _user_by_phone(session, phone)
    if user is None:
        raise AppError("account_not_found", 404)
    await verify_otp(session, settings, phone=phone, purpose="reset_password", code=code)
    raw = new_opaque_token()
    session.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=_token_hash(settings, "reset", raw),
            expires_at=_now() + timedelta(seconds=settings.password_reset_token_ttl_s),
        )
    )
    record_audit(
        session,
        action="auth.password_reset_code_verified",
        actor_type="user",
        actor_id=str(user.id),
        entity_type="user",
        entity_id=str(user.id),
        ip=client.ip,
    )
    await session.commit()
    bind_log_context(user_id=str(user.id))
    log.info("auth.password_reset_code_verified")
    return raw


async def reset_password(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    client: ClientInfo,
    *,
    reset_token: str,
    new_password: str,
) -> None:
    row = await session.scalar(
        select(PasswordResetToken)
        .where(PasswordResetToken.token_hash == _token_hash(settings, "reset", reset_token))
        .with_for_update()
    )
    # Same answer as the app's fake backend: start the reset again.
    if row is None or row.used_at is not None or row.expires_at <= _now():
        reason = "unknown" if row is None else ("used" if row.used_at else "expired")
        log.warning("auth.password_reset_rejected", reason=reason)
        raise AppError("otp_expired", 400)
    user = await session.get(User, row.user_id)
    if user is None:  # pragma: no cover
        raise AppError("otp_expired", 400)

    user.password_hash = hash_password(new_password)
    row.used_at = _now()
    # Every existing login ends: whoever knew the old password is out.
    await _revoke_all(session, user.id, "password_reset")
    record_audit(
        session,
        action="auth.password_reset",
        actor_type="user",
        actor_id=str(user.id),
        entity_type="user",
        entity_id=str(user.id),
        ip=client.ip,
        details={"user_agent": client.user_agent, "sessions_revoked": True},
    )
    await session.commit()
    await redis.delete(phone_key("login:failures", user.phone))
    bind_log_context(user_id=str(user.id))
    log.info("auth.password_reset_succeeded")
