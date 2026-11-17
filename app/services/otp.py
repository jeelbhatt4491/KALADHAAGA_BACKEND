"""One-time codes for sign-up and password reset.

Rules (match the app's fake backend):
- 4 digits, valid 5 minutes; sending a new code retires the previous one.
- Wrong code -> `invalid_otp` with `attempt` 1..5 of `max_attempts` 5; after the
  5th wrong try the code is locked -> `too_many_attempts` until a new one is sent.
- No live code, or an expired one -> `otp_expired`.
- Sending: 30 s cooldown per phone + purpose, at most 5 sends per phone per hour
  -> `too_many_attempts` with `retry_after`.
Codes are stored only as an HMAC of (purpose, phone, code).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.rate_limit import acquire_cooldown, hit_counter, phone_key, seconds_left
from app.core.security import hashes_match, hmac_hash, new_numeric_code
from app.db.models import OtpCode
from app.providers.sms import SmsMessage, SmsProvider
from app.services.audit import record_audit

OtpPurpose = Literal["signup", "reset_password"]

log = get_logger("kaladhaga.otp")


@dataclass(frozen=True, slots=True)
class OtpChallenge:
    code_length: int
    expires_in: int


def _hash(settings: Settings, purpose: str, phone: str, code: str) -> str:
    return hmac_hash(settings, f"otp:{purpose}:{phone}:{code}")


def _sms_text(code: str, purpose: OtpPurpose, ttl_minutes: int) -> str:
    # TODO(exotel): must match the registered DLT template word for word.
    action = "verify your number" if purpose == "signup" else "reset your password"
    return (
        f"{code} is your Kala Dhaga code to {action}. It expires in {ttl_minutes} minutes. "
        "Do not share it with anyone."
    )


async def send_otp(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    sms: SmsProvider,
    *,
    phone: str,
    purpose: OtpPurpose,
) -> OtpChallenge:
    cooldown = await acquire_cooldown(
        redis, phone_key(f"otp:cooldown:{purpose}", phone), settings.otp_resend_cooldown_s
    )
    if cooldown:
        log.warning("otp.send_throttled", phone=phone, purpose=purpose, reason="cooldown")
        raise AppError("too_many_attempts", 429, retry_after=cooldown)
    hourly_key = phone_key("otp:hourly", phone)
    if await hit_counter(redis, hourly_key, 3600) > settings.otp_max_sends_per_hour:
        log.warning("otp.send_throttled", phone=phone, purpose=purpose, reason="hourly_cap")
        raise AppError("too_many_attempts", 429, retry_after=await seconds_left(redis, hourly_key))

    now = datetime.now(UTC)
    await session.execute(
        update(OtpCode)
        .where(OtpCode.phone == phone, OtpCode.purpose == purpose, OtpCode.consumed_at.is_(None))
        .values(consumed_at=now)
    )
    code = settings.otp_fixed_code or new_numeric_code(settings.otp_length)
    otp = OtpCode(
        phone=phone,
        purpose=purpose,
        code_hash=_hash(settings, purpose, phone, code),
        expires_at=now + timedelta(seconds=settings.otp_ttl_s),
        attempts=0,
    )
    session.add(otp)
    record_audit(
        session,
        action="auth.otp_sent",
        actor_type="system",
        entity_type="otp",
        details={"phone": phone, "purpose": purpose},
    )
    await session.commit()

    # Sent after the commit, so a delivered code always exists in the database.
    try:
        result = await sms.send(
            SmsMessage(
                to=phone,
                body=_sms_text(code, purpose, settings.otp_ttl_s // 60),
                template_id=settings.sms_otp_template_id,
            )
        )
    except Exception as exc:
        log.exception("otp.sms_failed", phone=phone, purpose=purpose, challenge_id=str(otp.id))
        raise AppError("sms_unavailable", 503) from exc
    log.info(
        "otp.sent",
        phone=phone,
        purpose=purpose,
        challenge_id=str(otp.id),
        provider_message_id=result.provider_message_id,
    )
    return OtpChallenge(code_length=settings.otp_length, expires_in=settings.otp_ttl_s)


async def verify_otp(
    session: AsyncSession,
    settings: Settings,
    *,
    phone: str,
    purpose: OtpPurpose,
    code: str,
) -> None:
    """Checks the code and marks it used. The caller commits together with the
    change the code authorises (account creation, reset token)."""
    otp = await session.scalar(
        select(OtpCode)
        .where(OtpCode.phone == phone, OtpCode.purpose == purpose, OtpCode.consumed_at.is_(None))
        .order_by(OtpCode.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    now = datetime.now(UTC)
    if otp is None:
        log.warning("otp.verify_failed", phone=phone, purpose=purpose, reason="no_live_code")
        raise AppError("otp_expired", 400)
    if otp.attempts >= settings.otp_max_attempts:
        log.warning("otp.verify_failed", phone=phone, purpose=purpose, reason="locked")
        raise AppError("too_many_attempts", 429)
    if otp.expires_at <= now:
        log.warning("otp.verify_failed", phone=phone, purpose=purpose, reason="expired")
        raise AppError("otp_expired", 400)
    if not hashes_match(otp.code_hash, _hash(settings, purpose, phone, code)):
        otp.attempts += 1
        attempt = otp.attempts
        if attempt >= settings.otp_max_attempts:
            record_audit(
                session,
                action="auth.otp_locked",
                actor_type="system",
                entity_type="otp",
                entity_id=str(otp.id),
                details={"phone": phone, "purpose": purpose, "attempts": attempt},
            )
        # Committed before raising: the attempt count must survive the error.
        await session.commit()
        log.warning(
            "otp.verify_failed",
            phone=phone,
            purpose=purpose,
            reason="wrong_code",
            attempt=attempt,
            max_attempts=settings.otp_max_attempts,
        )
        raise AppError("invalid_otp", 400, attempt=attempt, max_attempts=settings.otp_max_attempts)
    otp.consumed_at = now
    log.info("otp.verified", phone=phone, purpose=purpose, challenge_id=str(otp.id))
