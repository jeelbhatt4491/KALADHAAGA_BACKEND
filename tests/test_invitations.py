"""/me/invites and /me/watching: the contact's side of an invitation."""

from __future__ import annotations

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog
from tests.helpers import (
    account_holder,
    add_contact,
    add_member,
    bearer,
    random_phone,
    seed_band,
    sign_up,
)


async def test_invited_contact_accepts_and_watches(
    client: AsyncClient, app: FastAPI, session: AsyncSession
) -> None:
    holder = await account_holder(client, app)
    member = await add_member(client, holder)
    band = await seed_band(app)
    await client.post(
        "/devices/claim",
        json={"device_id": band["band_id"], "member_id": member["id"]},
        headers=holder,
    )
    phone = random_phone()
    contact = (await add_contact(client, holder, member["id"], phone=phone)).json()
    await client.patch(
        f"/family/members/{member['id']}/contacts/{contact['id']}",
        json={"is_guardian": True},
        headers=holder,
    )

    # The contact signs up with the invited number; no role chosen yet.
    guest = bearer(await sign_up(client, app, phone))
    me = (await client.get("/me", headers=guest)).json()
    assert me["pending_invitation_count"] == 1 and me["onboarding_state"] == "needs_role"

    [invite] = (await client.get("/me/invites", headers=guest)).json()
    assert invite["id"] == contact["id"] and invite["member_name"] == "Asha"
    assert invite["is_guardian"] is True and invite["cohort"] == 1 and invite["invited_at"]

    assert (
        await client.post(f"/me/invites/{invite['id']}/accept", headers=guest)
    ).status_code == 204
    me = (await client.get("/me", headers=guest)).json()
    assert me["watching_count"] == 1 and me["pending_invitation_count"] == 0
    assert me["onboarding_state"] == "watching"  # accepting set the role to contact
    assert (await client.get("/me/invites", headers=guest)).json() == []

    [watched] = (await client.get("/me/watching", headers=guest)).json()
    assert watched["member_id"] == member["id"] and watched["is_guardian"] is True
    assert watched["band"]["id"] == band["band_id"]

    listed = (await client.get(f"/family/members/{member['id']}/contacts", headers=holder)).json()
    assert [c["invitation_status"] for c in listed if c["id"] == contact["id"]] == ["accepted"]
    # Accepting twice is fine; declining after accepting is not.
    assert (
        await client.post(f"/me/invites/{invite['id']}/accept", headers=guest)
    ).status_code == 204
    late = await client.post(f"/me/invites/{invite['id']}/decline", headers=guest)
    assert late.status_code == 409 and late.json()["code"] == "already_accepted"
    assert await session.scalar(
        select(AuditLog).where(
            AuditLog.action == "invite.accepted", AuditLog.entity_id == contact["id"]
        )
    )


async def test_decline_and_strangers(client: AsyncClient, app: FastAPI) -> None:
    holder = await account_holder(client, app)
    member = await add_member(client, holder)
    phone = random_phone()
    contact = (await add_contact(client, holder, member["id"], phone=phone)).json()
    guest = bearer(await sign_up(client, app, phone))
    stranger = await account_holder(client, app)

    for path in ("accept", "decline"):
        r = await client.post(f"/me/invites/{contact['id']}/{path}", headers=stranger)
        assert r.status_code == 404 and r.json()["code"] == "not_found"
    # The account holder's own entry is never an invitation.
    [ah] = [
        c
        for c in (
            await client.get(f"/family/members/{member['id']}/contacts", headers=holder)
        ).json()
        if c["is_account_holder"]
    ]
    assert (await client.post(f"/me/invites/{ah['id']}/accept", headers=holder)).status_code == 404

    assert (
        await client.post(f"/me/invites/{contact['id']}/decline", headers=guest)
    ).status_code == 204
    me = (await client.get("/me", headers=guest)).json()
    assert me["pending_invitation_count"] == 0 and me["watching_count"] == 0
    listed = (await client.get(f"/family/members/{member['id']}/contacts", headers=holder)).json()
    assert [c["invitation_status"] for c in listed if c["id"] == contact["id"]] == ["declined"]
