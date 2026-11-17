"""/devices: availability, vibrate check + pair-ack + confirm, claim, deactivate."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog
from tests.helpers import account_holder, add_member, band_post, random_band_id, seed_band


def _pair_nonce(app: FastAPI, band: dict[str, Any]) -> str:
    for message in reversed(app.state.providers.sms.sent):
        if message.to == band["sim_msisdn"] and message.body.startswith("KD1 PAIR "):
            return str(message.body.split()[-1])
    raise AssertionError("no KD1 PAIR sent to the band")


async def test_availability_errors(client: AsyncClient, app: FastAPI) -> None:
    auth = await account_holder(client, app)
    missing = await client.get(f"/devices/{random_band_id()}/availability", headers=auth)
    assert missing.status_code == 404 and missing.json()["code"] == "not_found"

    async with app.state.session_factory() as session:
        from app.services.devices import seed_bands

        [unsold] = await seed_bands(
            session, app.state.settings, [random_band_id()], status="unsold"
        )
    not_sold = await client.get(f"/devices/{unsold.band_id}/availability", headers=auth)
    assert not_sold.status_code == 409 and not_sold.json()["code"] == "not_sold"

    band = await seed_band(app)
    ok = await client.get(f"/devices/{band['band_id'].lower()}/availability", headers=auth)
    assert ok.status_code == 200
    assert ok.json() == {
        "battery_percent": None,
        "firmware_version": None,
        "firmware_up_to_date": None,
    }

    member = await add_member(client, auth)
    await client.post(
        "/devices/claim",
        json={"device_id": band["band_id"], "member_id": member["id"]},
        headers=auth,
    )
    active = await client.get(f"/devices/{band['band_id']}/availability", headers=auth)
    assert active.status_code == 409 and active.json()["code"] == "already_active"
    assert active.json()["linked_at"]


async def test_vibrate_check_pair_ack_confirm_and_claim(
    client: AsyncClient, app: FastAPI, session: AsyncSession
) -> None:
    auth = await account_holder(client, app)
    member = await add_member(client, auth)
    band = await seed_band(app)

    check = await client.post(f"/devices/{band['band_id']}/vibrate-check", headers=auth)
    assert check.status_code == 202 and check.json()["expires_in"] == 300
    again = await client.post(f"/devices/{band['band_id']}/vibrate-check", headers=auth)
    assert again.status_code == 429 and again.json()["retry_after"] > 0

    wrong = await band_post(client, band, "/device/v1/pair-ack", {"nonce": "00000000"})
    assert wrong.status_code == 400 and wrong.json()["code"] == "invalid_nonce"
    ack = await band_post(client, band, "/device/v1/pair-ack", {"nonce": _pair_nonce(app, band)})
    assert ack.status_code == 204

    confirm = await client.post(
        f"/devices/{band['band_id']}/vibrate-check/confirm", json={"felt": True}, headers=auth
    )
    assert confirm.status_code == 200 and confirm.json() == {"band_acked": True}

    claimed = await client.post(
        "/devices/claim",
        json={"device_id": band["band_id"], "member_id": member["id"]},
        headers=auth,
    )
    assert claimed.status_code == 200, claimed.text
    body = claimed.json()
    assert body["id"] == member["id"] and body["band"]["id"] == band["band_id"]
    assert body["band"]["is_connected"] is False  # never heard from yet
    # Account holder is the Cohort 1 guardian, so the band is ready at once.
    assert body["ready"] is True and body["missing"] == []

    audit = await session.scalar(
        select(AuditLog).where(
            AuditLog.action == "device.claimed", AuditLog.entity_id == band["band_id"]
        )
    )
    assert audit is not None and audit.details["proof"] == "felt_confirmed"
    acked = await session.scalar(
        select(AuditLog).where(
            AuditLog.action == "device.pair_acked", AuditLog.entity_id == band["band_id"]
        )
    )
    assert acked is not None and acked.actor_type == "device"


async def test_confirm_without_vibrate_check(client: AsyncClient, app: FastAPI) -> None:
    auth = await account_holder(client, app)
    band = await seed_band(app)
    response = await client.post(
        f"/devices/{band['band_id']}/vibrate-check/confirm", json={"felt": True}, headers=auth
    )
    assert response.status_code == 409 and response.json()["code"] == "pairing_expired"


async def test_one_band_per_member_and_bands_are_not_shared(
    client: AsyncClient, app: FastAPI
) -> None:
    auth = await account_holder(client, app)
    other = await account_holder(client, app)
    member = await add_member(client, auth)
    other_member = await add_member(client, other)
    first, second = await seed_band(app), await seed_band(app)

    async def claim(band: dict[str, Any], member_id: str, headers: dict[str, str]) -> Response:
        return await client.post(
            "/devices/claim",
            json={"device_id": band["band_id"], "member_id": member_id},
            headers=headers,
        )

    assert (await claim(first, member["id"], auth)).status_code == 200
    twice = await claim(second, member["id"], auth)
    assert twice.status_code == 409 and twice.json()["code"] == "member_has_band"
    taken = await claim(first, other_member["id"], other)
    assert taken.status_code == 409 and taken.json()["code"] == "already_active"
    not_mine = await claim(second, member["id"], other)
    assert not_mine.status_code == 404  # someone else's member

    # Only the owner can deactivate; afterwards the band can be paired again.
    stranger = await client.post(f"/devices/{first['band_id']}/deactivate", headers=other)
    assert stranger.status_code == 404
    done = await client.post(f"/devices/{first['band_id']}/deactivate", headers=auth)
    assert done.status_code == 204
    listed = (await client.get("/family/members", headers=auth)).json()
    assert listed[0]["band"] is None and listed[0]["missing"] == ["band"]
    assert (
        await client.get(f"/devices/{first['band_id']}/availability", headers=auth)
    ).status_code == 200
    assert (await claim(first, other_member["id"], other)).status_code == 200


async def test_dev_seed_endpoint(client: AsyncClient) -> None:
    body = {"prefix": "KZ-", "start": 1, "count": 2, "reset": True}
    response = await client.post("/dev/bands", json=body)
    assert response.status_code == 200
    bands = response.json()
    assert [b["band_id"] for b in bands] == ["KZ-0001", "KZ-0002"]
    assert bands[0]["sim_msisdn"] == "+917000000001" and bands[0]["secret"]
    again = (await client.post("/dev/bands", json={**body, "reset": False})).json()
    assert again[0]["secret"] is None  # existing band left as it was
