"""/me: onboarding state, role, profile, notification settings, push tokens."""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import create_access_token
from app.db.models import AuditLog, PushToken, User
from tests.helpers import bearer, sign_up


async def test_me_requires_a_valid_token(client: AsyncClient, settings: Settings) -> None:
    expired = create_access_token(
        Settings(access_token_ttl_s=-10, jwt_secret=settings.jwt_secret), uuid.uuid4()
    )
    for headers in (
        {},
        {"Authorization": "Bearer not-a-jwt"},
        {"Authorization": f"Bearer {expired}"},
        {"Authorization": f"Bearer {create_access_token(settings, uuid.uuid4())}"},
    ):
        response = await client.get("/me", headers=headers)
        assert response.status_code == 401
        assert response.json()["code"] == "unauthorized"


async def test_onboarding_state_follows_role(client: AsyncClient, app: FastAPI, phone: str) -> None:
    auth = bearer(await sign_up(client, app, phone))

    async def state() -> str:
        body = (await client.get("/me", headers=auth)).json()
        assert body["family_member_count"] == 0 and body["watching_count"] == 0
        return str(body["onboarding_state"])

    assert await state() == "needs_role"
    role = await client.post("/me/role", json={"role": "account_holder"}, headers=auth)
    assert role.status_code == 204
    assert await state() == "needs_first_member"
    await client.post("/me/role", json={"role": "contact"}, headers=auth)
    assert await state() == "watching"
    bad = await client.post("/me/role", json={"role": "admin"}, headers=auth)
    assert bad.status_code == 422


async def test_profile_update(
    client: AsyncClient, app: FastAPI, session: AsyncSession, phone: str
) -> None:
    auth = bearer(await sign_up(client, app, phone))
    empty = (await client.get("/me/profile", headers=auth)).json()
    assert empty == {
        "phone": phone,
        "name": None,
        "date_of_birth": None,
        "gender": None,
        "city": None,
    }

    full = await client.patch(
        "/me/profile",
        json={
            "name": "  Asha  ",
            "date_of_birth": "1990-04-12",
            "gender": "female",
            "city": "Surat",
        },
        headers=auth,
    )
    partial = await client.patch("/me/profile", json={"city": "Ahmedabad"}, headers=auth)
    cleared = await client.patch("/me/profile", json={"gender": None}, headers=auth)

    assert full.status_code == 200 and full.json()["name"] == "Asha"
    assert partial.json()["name"] == "Asha" and partial.json()["city"] == "Ahmedabad"
    assert cleared.json()["gender"] is None and cleared.json()["date_of_birth"] == "1990-04-12"

    user = await session.scalar(select(User).where(User.phone == phone))
    assert user is not None
    details = (
        await session.scalars(
            select(AuditLog.details)
            .where(AuditLog.entity_id == str(user.id), AuditLog.action == "me.profile_updated")
            .order_by(AuditLog.id)
        )
    ).all()
    assert details[0] == {"fields": ["city", "date_of_birth", "gender", "name"]}  # names only


async def test_profile_validation(client: AsyncClient, app: FastAPI, phone: str) -> None:
    auth = bearer(await sign_up(client, app, phone))
    for body in (
        {"date_of_birth": "2999-01-01"},
        {"gender": "unknown"},
        {"name": ""},
        {"name": "x" * 101},
    ):
        response = await client.patch("/me/profile", json=body, headers=auth)
        assert response.status_code == 422, body


async def test_notification_settings(client: AsyncClient, app: FastAPI, phone: str) -> None:
    auth = bearer(await sign_up(client, app, phone))
    defaults = (await client.get("/me/notification-settings", headers=auth)).json()
    assert defaults == {
        "emergency_alerts": True,
        "band_offline": True,
        "low_battery": True,
        "test_reminders": True,
        "firmware_updates": True,
    }

    saved = await client.put(
        "/me/notification-settings",
        json={**defaults, "emergency_alerts": False, "low_battery": False},
        headers=auth,
    )
    assert saved.status_code == 204
    after = (await client.get("/me/notification-settings", headers=auth)).json()
    assert after["low_battery"] is False
    assert after["emergency_alerts"] is True  # locked on


async def test_push_tokens(
    client: AsyncClient, app: FastAPI, session: AsyncSession, phone: str
) -> None:
    first = await sign_up(client, app, phone)
    other_phone = "+9197" + phone[5:]
    second = await sign_up(client, app, other_phone)
    token = f"fcm-{uuid.uuid4().hex}"
    body = {"token": token, "platform": "android"}

    assert (
        await client.post("/me/push-tokens", json=body, headers=bearer(first))
    ).status_code == 204
    assert (
        await client.post("/me/push-tokens", json=body, headers=bearer(first))
    ).status_code == 204
    # The phone signs in to another account: the token moves with it.
    await client.post("/me/push-tokens", json=body, headers=bearer(second))
    owner = await session.scalar(select(PushToken.user_id).where(PushToken.token == token))
    second_user = await session.scalar(select(User.id).where(User.phone == other_phone))
    assert owner == second_user

    await client.post(
        "/auth/logout", json={"refresh_token": second["refresh_token"], "push_token": token}
    )
    session.expire_all()
    assert await session.scalar(select(PushToken).where(PushToken.token == token)) is None

    bad = await client.post(
        "/me/push-tokens", json={"token": token, "platform": "windows"}, headers=bearer(first)
    )
    assert bad.status_code == 422
