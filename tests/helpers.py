"""Helpers shared by the API tests."""

from __future__ import annotations

import re

from fastapi import FastAPI
from httpx import AsyncClient

PASSWORD = "kala1234"
_CODE = re.compile(r"\b(\d{4})\b")


def last_otp(app: FastAPI, phone: str) -> str:
    """The code most recently 'texted' to `phone` by the fake SMS provider."""
    for message in reversed(app.state.providers.sms.sent):
        if message.to == phone:
            match = _CODE.search(message.body)
            assert match, message.body
            return match.group(1)
    raise AssertionError(f"no SMS sent to {phone}")


async def sign_up(client: AsyncClient, app: FastAPI, phone: str) -> dict[str, str]:
    """Full sign-up flow; returns the token pair."""
    sent = await client.post("/auth/otp/send", json={"phone": phone, "purpose": "signup"})
    assert sent.status_code == 200, sent.text
    response = await client.post(
        "/auth/signup/verify",
        json={"phone": phone, "password": PASSWORD, "code": last_otp(app, phone)},
    )
    assert response.status_code == 201, response.text
    tokens: dict[str, str] = response.json()
    return tokens


def bearer(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}
