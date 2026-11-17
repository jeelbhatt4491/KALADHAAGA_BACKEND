"""/device/v1: request signing, replay and clock checks, heartbeat."""

from __future__ import annotations

import json
import time

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Device
from tests.helpers import account_holder, add_member, band_post, seed_band, signed_headers

HEARTBEAT = {"battery_percent": 76, "signal": "good", "firmware_version": "1.2.0"}


async def test_heartbeat_updates_band_status(client: AsyncClient, app: FastAPI) -> None:
    auth = await account_holder(client, app)
    member = await add_member(client, auth)
    band = await seed_band(app)
    await client.post(
        "/devices/claim",
        json={"device_id": band["band_id"], "member_id": member["id"]},
        headers=auth,
    )

    response = await band_post(client, band, "/device/v1/heartbeat", HEARTBEAT)
    assert response.status_code == 200 and abs(response.json()["server_time"] - time.time()) < 5

    [listed] = (await client.get("/family/members", headers=auth)).json()
    assert listed["band"]["battery_percent"] == 76 and listed["band"]["signal"] == "good"
    assert listed["band"]["is_connected"] is True and listed["band"]["firmware_version"] == "1.2.0"
    assert listed["band"]["last_seen_at"]


async def test_signature_checks(client: AsyncClient, app: FastAPI) -> None:
    band = await seed_band(app)
    body = json.dumps(HEARTBEAT).encode()
    url = "/device/v1/heartbeat"

    async def code(headers: dict[str, str], content: bytes = body) -> tuple[int, str]:
        r = await client.post(url, content=content, headers=headers)
        return r.status_code, r.json()["code"]

    assert await code({"Content-Type": "application/json"}) == (401, "invalid_signature")
    wrong_secret = {**band, "secret": "0" * 64}
    assert await code(signed_headers(wrong_secret, body)) == (401, "invalid_signature")
    unknown = {**band, "band_id": "KD-NOPE0000"}
    assert await code(signed_headers(unknown, body)) == (401, "invalid_signature")
    stale = signed_headers(band, body, timestamp=int(time.time()) - 3600)
    assert await code(stale) == (401, "invalid_signature")
    # Body changed after signing.
    tampered = json.dumps({**HEARTBEAT, "battery_percent": 1}).encode()
    assert await code(signed_headers(band, body), tampered) == (401, "invalid_signature")

    headers = signed_headers(band, body)
    assert (await client.post(url, content=body, headers=headers)).status_code == 200
    assert await code(headers) == (401, "invalid_signature")  # replayed

    bad = json.dumps({"battery_percent": 150, "signal": "great"}).encode()
    invalid = await client.post(url, content=bad, headers=signed_headers(band, bad))
    assert invalid.status_code == 422 and invalid.json()["code"] == "validation_error"


async def test_device_secret_is_encrypted_at_rest(app: FastAPI, session: AsyncSession) -> None:
    band = await seed_band(app)
    device = await session.scalar(select(Device).where(Device.band_id == band["band_id"]))
    assert device is not None
    assert band["secret"].encode() not in device.secret_ciphertext
