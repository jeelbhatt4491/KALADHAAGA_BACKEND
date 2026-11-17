"""/auth: sign-up, OTP rules, login, refresh rotation, logout, password reset."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.rate_limit import phone_key
from app.db.models import AuditLog, OtpCode, RefreshToken, User
from tests.helpers import PASSWORD, bearer, last_otp, sign_up


async def _audit_actions(session: AsyncSession, **where: str) -> list[str]:
    query = select(AuditLog.action).order_by(AuditLog.id)
    for key, value in where.items():
        query = query.where(getattr(AuditLog, key) == value)
    return list((await session.scalars(query)).all())


async def _clear_cooldown(app: FastAPI, phone: str, purpose: str = "signup") -> None:
    await app.state.redis.delete(phone_key(f"otp:cooldown:{purpose}", phone))


# ---------------------------------------------------------------------------- sign-up


async def test_signup_flow(
    client: AsyncClient, app: FastAPI, session: AsyncSession, phone: str
) -> None:
    sent = await client.post("/auth/otp/send", json={"phone": phone, "purpose": "signup"})
    assert sent.status_code == 200
    assert sent.json() == {"code_length": 4, "expires_in": 300}

    response = await client.post(
        "/auth/signup/verify",
        json={"phone": phone, "password": PASSWORD, "code": last_otp(app, phone)},
    )

    assert response.status_code == 201
    tokens = response.json()
    me = await client.get("/me", headers=bearer(tokens))
    assert me.status_code == 200
    assert me.json()["phone"] == phone
    assert me.json()["onboarding_state"] == "needs_role"

    user = await session.scalar(select(User).where(User.phone == phone))
    assert user is not None and user.phone_verified_at is not None
    assert user.password_hash.startswith("$argon2id$")
    actions = await _audit_actions(session, entity_id=str(user.id))
    assert actions == ["auth.signup"]


@pytest.mark.parametrize(
    "typed", ["98765 43210", "+91 98765-43210", "0091 9876543210", "09876543210"]
)
async def test_phone_formats_are_normalised(client: AsyncClient, app: FastAPI, typed: str) -> None:
    response = await client.post("/auth/otp/send", json={"phone": typed, "purpose": "signup"})

    assert response.status_code in (200, 409)  # 409 if a previous run created it
    if response.status_code == 200:
        assert app.state.providers.sms.sent[-1].to == "+919876543210"


async def test_invalid_phone_is_rejected(client: AsyncClient) -> None:
    response = await client.post("/auth/otp/send", json={"phone": "12345", "purpose": "signup"})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_weak_password_is_rejected(client: AsyncClient, app: FastAPI, phone: str) -> None:
    await client.post("/auth/otp/send", json={"phone": phone, "purpose": "signup"})
    response = await client.post(
        "/auth/signup/verify",
        json={"phone": phone, "password": "abcdefgh", "code": last_otp(app, phone)},
    )
    assert response.status_code == 422


async def test_signup_code_for_existing_account_is_refused(
    client: AsyncClient, app: FastAPI, phone: str
) -> None:
    await sign_up(client, app, phone)
    response = await client.post("/auth/otp/send", json={"phone": phone, "purpose": "signup"})
    assert response.status_code == 409
    assert response.json()["code"] == "account_exists"


async def test_reset_code_for_unknown_account_is_refused(client: AsyncClient, phone: str) -> None:
    response = await client.post(
        "/auth/otp/send", json={"phone": phone, "purpose": "reset_password"}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "account_not_found"


# ---------------------------------------------------------------------------- otp rules


async def test_wrong_codes_count_up_then_lock(
    client: AsyncClient, app: FastAPI, phone: str
) -> None:
    await client.post("/auth/otp/send", json={"phone": phone, "purpose": "signup"})
    good = last_otp(app, phone)
    bad = "0000" if good != "0000" else "1111"
    body = {"phone": phone, "password": PASSWORD, "code": bad}

    for attempt in range(1, 6):
        response = await client.post("/auth/signup/verify", json=body)
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_otp"
        assert response.json()["attempt"] == attempt
        assert response.json()["max_attempts"] == 5

    locked = await client.post("/auth/signup/verify", json={**body, "code": good})
    assert locked.status_code == 429
    assert locked.json()["code"] == "too_many_attempts"


async def test_expired_code(
    client: AsyncClient, app: FastAPI, session: AsyncSession, phone: str
) -> None:
    await client.post("/auth/otp/send", json={"phone": phone, "purpose": "signup"})
    await session.execute(
        update(OtpCode)
        .where(OtpCode.phone == phone)
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await session.commit()

    response = await client.post(
        "/auth/signup/verify",
        json={"phone": phone, "password": PASSWORD, "code": last_otp(app, phone)},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "otp_expired"


async def test_resend_cooldown_and_new_code_replaces_old(
    client: AsyncClient, app: FastAPI, phone: str
) -> None:
    body = {"phone": phone, "purpose": "signup"}
    await client.post("/auth/otp/send", json=body)
    first = last_otp(app, phone)

    again = await client.post("/auth/otp/send", json=body)
    assert again.status_code == 429
    assert again.json()["code"] == "too_many_attempts"
    assert 0 < again.json()["retry_after"] <= 30
    assert again.headers["retry-after"] == str(again.json()["retry_after"])

    await _clear_cooldown(app, phone)
    assert (await client.post("/auth/otp/send", json=body)).status_code == 200
    second = last_otp(app, phone)
    if first != second:  # 1 in 10,000 they collide
        old = await client.post(
            "/auth/signup/verify", json={"phone": phone, "password": PASSWORD, "code": first}
        )
        assert old.json()["code"] == "invalid_otp"


async def test_hourly_send_cap(client: AsyncClient, app: FastAPI, phone: str) -> None:
    body = {"phone": phone, "purpose": "signup"}
    for _ in range(5):
        assert (await client.post("/auth/otp/send", json=body)).status_code == 200
        await _clear_cooldown(app, phone)

    capped = await client.post("/auth/otp/send", json=body)
    assert capped.status_code == 429
    assert capped.json()["retry_after"] > 30


# ---------------------------------------------------------------------------- login


async def test_login(client: AsyncClient, app: FastAPI, session: AsyncSession, phone: str) -> None:
    await sign_up(client, app, phone)

    ok = await client.post("/auth/login", json={"phone": phone, "password": PASSWORD})
    wrong = await client.post("/auth/login", json={"phone": phone, "password": "nope12345"})
    unknown = await client.post(
        "/auth/login", json={"phone": "+919999999999", "password": PASSWORD}
    )

    assert ok.status_code == 200 and set(ok.json()) == {"access_token", "refresh_token"}
    assert wrong.status_code == 401 and wrong.json()["code"] == "invalid_credentials"
    assert unknown.status_code == 404 and unknown.json()["code"] == "account_not_found"
    user = await session.scalar(select(User).where(User.phone == phone))
    assert user is not None
    actions = await _audit_actions(session, entity_id=str(user.id))
    assert actions[-2:] == ["auth.login", "auth.login_failed"]


async def test_login_locks_after_repeated_failures(
    client: AsyncClient, app: FastAPI, phone: str
) -> None:
    await sign_up(client, app, phone)
    for _ in range(4):
        response = await client.post("/auth/login", json={"phone": phone, "password": "bad12345"})
        assert response.status_code == 401

    fifth = await client.post("/auth/login", json={"phone": phone, "password": "bad12345"})
    blocked = await client.post("/auth/login", json={"phone": phone, "password": PASSWORD})

    assert fifth.status_code == 429 and fifth.json()["retry_after"] == 900
    assert blocked.status_code == 429  # even the right password, until the lock ends


# ---------------------------------------------------------------------------- tokens


async def test_refresh_rotates_tokens(client: AsyncClient, app: FastAPI, phone: str) -> None:
    tokens = await sign_up(client, app, phone)

    response = await client.post(
        "/auth/token/refresh", json={"refresh_token": tokens["refresh_token"]}
    )

    assert response.status_code == 200
    new = response.json()
    assert new["refresh_token"] != tokens["refresh_token"]
    assert (await client.get("/me", headers=bearer(new))).status_code == 200


async def test_reused_refresh_token_revokes_the_whole_login(
    client: AsyncClient, app: FastAPI, session: AsyncSession, phone: str
) -> None:
    tokens = await sign_up(client, app, phone)
    rotated = (
        await client.post("/auth/token/refresh", json={"refresh_token": tokens["refresh_token"]})
    ).json()

    replay = await client.post(
        "/auth/token/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    after = await client.post(
        "/auth/token/refresh", json={"refresh_token": rotated["refresh_token"]}
    )

    assert replay.status_code == 401 and replay.json()["code"] == "invalid_refresh_token"
    assert after.status_code == 401  # the legitimate new token is dead too
    assert "auth.refresh_reuse_detected" in await _audit_actions(session)


async def test_expired_refresh_token(
    client: AsyncClient, app: FastAPI, session: AsyncSession, phone: str
) -> None:
    tokens = await sign_up(client, app, phone)
    user = await session.scalar(select(User).where(User.phone == phone))
    assert user is not None
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id)
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await session.commit()

    response = await client.post(
        "/auth/token/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert response.status_code == 401


async def test_logout_revokes_refresh_token(client: AsyncClient, app: FastAPI, phone: str) -> None:
    tokens = await sign_up(client, app, phone)

    out = await client.post("/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    again = await client.post("/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    refresh = await client.post(
        "/auth/token/refresh", json={"refresh_token": tokens["refresh_token"]}
    )

    assert out.status_code == 204 and again.status_code == 204
    assert refresh.status_code == 401


# ---------------------------------------------------------------------------- password reset


async def test_password_reset_flow(
    client: AsyncClient, app: FastAPI, session: AsyncSession, phone: str
) -> None:
    tokens = await sign_up(client, app, phone)
    sent = await client.post("/auth/otp/send", json={"phone": phone, "purpose": "reset_password"})
    assert sent.status_code == 200
    verify = await client.post(
        "/auth/password/reset/verify", json={"phone": phone, "code": last_otp(app, phone)}
    )
    assert verify.status_code == 200
    reset_token = verify.json()["reset_token"]

    done = await client.post(
        "/auth/password/reset", json={"reset_token": reset_token, "new_password": "newpass99"}
    )
    reused = await client.post(
        "/auth/password/reset", json={"reset_token": reset_token, "new_password": "other1234"}
    )

    assert done.status_code == 204
    assert reused.status_code == 400 and reused.json()["code"] == "otp_expired"
    old_login = await client.post("/auth/login", json={"phone": phone, "password": PASSWORD})
    new_login = await client.post("/auth/login", json={"phone": phone, "password": "newpass99"})
    old_refresh = await client.post(
        "/auth/token/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert old_login.status_code == 401
    assert new_login.status_code == 200
    assert old_refresh.status_code == 401  # every earlier login was ended
    assert "auth.password_reset" in await _audit_actions(session)


# ---------------------------------------------------------------------------- logging


async def test_auth_logs_never_contain_codes_phones_or_tokens(
    client: AsyncClient, app: FastAPI, tmp_path: Path, phone: str
) -> None:
    configure_logging(
        Settings(log_to_file=True, log_dir=tmp_path, log_format="json"), service="api"
    )
    try:
        tokens = await sign_up(client, app, phone)
        await client.post("/auth/login", json={"phone": phone, "password": PASSWORD})
        await client.post("/auth/token/refresh", json={"refresh_token": tokens["refresh_token"]})
        code = last_otp(app, phone)
        for handler in logging.getLogger().handlers:
            handler.flush()
        text = "".join(
            p.read_text(encoding="utf-8")
            for p in tmp_path.glob("*.log")  # noqa: ASYNC240
        )
    finally:
        configure_logging(get_settings(), service="api")

    events = [json.loads(line)["event"] for line in text.splitlines()]
    assert {"otp.sent", "auth.signup_succeeded", "auth.login_succeeded"} <= set(events)
    assert phone[3:] not in text  # the 10-digit number
    assert f" {code} " not in text and f'"{code}"' not in text
    assert PASSWORD not in text
    assert tokens["refresh_token"] not in text and tokens["access_token"] not in text
