"""Helpers shared by the API tests."""

from __future__ import annotations

import json
import re
import secrets
import time
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient, Response

from app.core.device_auth import sign_device_request
from app.services.devices import seed_bands

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


# ---------------------------------------------------------------------------- B2 helpers

# Smallest valid PNG (1x1), for photo uploads.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


def random_phone() -> str:
    return f"+9197{secrets.randbelow(10**8):08d}"


def random_band_id() -> str:
    return f"KT-{secrets.token_hex(4).upper()}"


async def account_holder(
    client: AsyncClient, app: FastAPI, phone: str | None = None
) -> dict[str, str]:
    """Signed-up user who chose "Set up my band"; returns auth headers."""
    auth = bearer(await sign_up(client, app, phone or random_phone()))
    response = await client.post("/me/role", json={"role": "account_holder"}, headers=auth)
    assert response.status_code == 204, response.text
    return auth


async def add_member(
    client: AsyncClient,
    auth: dict[str, str],
    *,
    name: str = "Asha",
    relationship: str = "daughter",
    age: int = 14,
    gender: str = "female",
    photo: bytes | None = None,
) -> dict[str, Any]:
    files = {"photo": ("photo.png", photo, "image/png")} if photo is not None else None
    response = await client.post(
        "/family/members",
        data={
            "name": name,
            "relationship": relationship,
            "age": str(age),
            "gender": gender,
            "consent": "true",
        },
        files=files,
        headers=auth,
    )
    assert response.status_code == 201, response.text
    member: dict[str, Any] = response.json()
    return member


async def seed_band(app: FastAPI, band_id: str | None = None) -> dict[str, Any]:
    """A sold band, ready to claim; returns {band_id, sim_msisdn, secret}."""
    async with app.state.session_factory() as session:
        [band] = await seed_bands(session, app.state.settings, [band_id or random_band_id()])
    return {"band_id": band.band_id, "sim_msisdn": band.sim_msisdn, "secret": band.secret}


async def add_contact(
    client: AsyncClient,
    auth: dict[str, str],
    member_id: str,
    *,
    cohort: int = 1,
    phone: str | None = None,
    name: str = "Ravi",
    proximity: str = "strong",
) -> Response:
    return await client.post(
        f"/family/members/{member_id}/contacts",
        json={
            "name": name,
            "phone": phone or random_phone(),
            "relationship": "uncle",
            "city": "Ahmedabad",
            "proximity": proximity,
            "availability": "available",
            "cohort": cohort,
        },
        headers=auth,
    )


def signed_headers(
    band: dict[str, Any], body: bytes, timestamp: int | None = None
) -> dict[str, str]:
    ts = str(timestamp if timestamp is not None else int(time.time()))
    return {
        "X-Device-Id": band["band_id"],
        "X-Timestamp": ts,
        "X-Signature": sign_device_request(band["secret"], ts, body),
        "Content-Type": "application/json",
    }


async def band_post(
    client: AsyncClient, band: dict[str, Any], path: str, payload: dict[str, Any]
) -> Response:
    body = json.dumps(payload).encode()
    return await client.post(path, content=body, headers=signed_headers(band, body))
