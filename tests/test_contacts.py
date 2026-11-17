"""/family/members/{id}/contacts: cohorts, order, guardians, copy, invites, ready."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, Contact
from tests.helpers import account_holder, add_contact, add_member, random_phone, seed_band


async def _contacts(
    client: AsyncClient, auth: dict[str, str], member_id: str
) -> list[dict[str, Any]]:
    response = await client.get(f"/family/members/{member_id}/contacts", headers=auth)
    assert response.status_code == 200
    return list(response.json())


async def test_add_contact_sends_invite_and_keeps_order(client: AsyncClient, app: FastAPI) -> None:
    auth = await account_holder(client, app)
    member = await add_member(client, auth)
    phone = random_phone()

    created = await add_contact(client, auth, member["id"], phone="0" + phone[3:])
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["phone"] == phone  # normalised like the app does
    assert body["priority"] == 2 and body["invitation_status"] == "pending"
    assert not body["is_guardian"]
    [invite] = [m for m in app.state.providers.sms.sent if m.to == phone]
    assert "Asha" in invite.body and "emergency contact" in invite.body

    second = (await add_contact(client, auth, member["id"], cohort=2)).json()
    listed = await _contacts(client, auth, member["id"])
    assert [(c["cohort"], c["priority"]) for c in listed] == [(1, 1), (1, 2), (2, 1)]
    assert listed[2]["id"] == second["id"]


async def test_cohort_full_and_duplicate_phone(client: AsyncClient, app: FastAPI) -> None:
    auth = await account_holder(client, app)
    member = await add_member(client, auth)
    phone = random_phone()
    assert (await add_contact(client, auth, member["id"], phone=phone)).status_code == 201
    dup = await add_contact(client, auth, member["id"], phone=phone, cohort=2)
    assert dup.status_code == 409 and dup.json()["code"] == "duplicate_phone"

    for _ in range(3):  # account holder + 4 = 5
        assert (await add_contact(client, auth, member["id"])).status_code == 201
    full = await add_contact(client, auth, member["id"])
    assert full.status_code == 409 and full.json()["code"] == "cohort_full"

    # Moving into a full cohort is refused too.
    other = (await add_contact(client, auth, member["id"], cohort=2)).json()
    move = await client.patch(
        f"/family/members/{member['id']}/contacts/{other['id']}", json={"cohort": 1}, headers=auth
    )
    assert move.status_code == 409 and move.json()["code"] == "cohort_full"


async def test_guardian_rules(client: AsyncClient, app: FastAPI, session: AsyncSession) -> None:
    auth = await account_holder(client, app)
    member = await add_member(client, auth)
    [ah] = await _contacts(client, auth, member["id"])
    a = (await add_contact(client, auth, member["id"])).json()
    b = (await add_contact(client, auth, member["id"], cohort=2)).json()
    url = f"/family/members/{member['id']}/contacts"

    # The only guardian can't be removed or unset.
    last = await client.delete(f"{url}/{ah['id']}", headers=auth)
    assert last.status_code == 409 and last.json()["code"] == "last_guardian"
    unset = await client.patch(f"{url}/{ah['id']}", json={"is_guardian": False}, headers=auth)
    assert unset.status_code == 409 and unset.json()["code"] == "last_guardian"

    made = await client.patch(f"{url}/{a['id']}", json={"is_guardian": True}, headers=auth)
    assert made.status_code == 200 and made.json()["is_guardian"]
    assert made.json()["invitation_status"] == "pending"  # guardian without accepting yet
    limit = await client.patch(f"{url}/{b['id']}", json={"is_guardian": True}, headers=auth)
    assert limit.status_code == 409 and limit.json()["code"] == "guardian_limit"

    # Now there are two, the account holder may step back.
    assert (await client.delete(f"{url}/{ah['id']}", headers=auth)).status_code == 204
    remaining = await _contacts(client, auth, member["id"])
    assert [(c["id"], c["priority"]) for c in remaining if c["cohort"] == 1] == [(a["id"], 1)]

    audit = await session.scalar(
        select(AuditLog).where(AuditLog.action == "contact.updated", AuditLog.entity_id == a["id"])
    )
    assert audit is not None and audit.details["is_guardian"] == {"from": False, "to": True}
    removed = await session.scalar(
        select(AuditLog).where(AuditLog.action == "contact.removed", AuditLog.entity_id == ah["id"])
    )
    assert removed is not None and "******" in removed.details["phone"]


async def test_move_reorder_and_phone_change(client: AsyncClient, app: FastAPI) -> None:
    auth = await account_holder(client, app)
    member = await add_member(client, auth)
    url = f"/family/members/{member['id']}/contacts"
    [ah] = await _contacts(client, auth, member["id"])
    a = (await add_contact(client, auth, member["id"])).json()
    b = (await add_contact(client, auth, member["id"])).json()

    order = [b["id"], ah["id"], a["id"]]
    assert (
        await client.patch(f"{url}/reorder", json={"cohort": 1, "ids": order}, headers=auth)
    ).status_code == 204
    assert [c["id"] for c in await _contacts(client, auth, member["id"])] == order
    bad = await client.patch(f"{url}/reorder", json={"cohort": 1, "ids": order[:2]}, headers=auth)
    assert bad.status_code == 400 and bad.json()["code"] == "invalid_order"

    moved = await client.patch(f"{url}/{ah['id']}", json={"cohort": 2}, headers=auth)
    assert (
        moved.status_code == 200 and moved.json()["cohort"] == 2 and moved.json()["priority"] == 1
    )
    assert [
        (c["id"], c["priority"])
        for c in await _contacts(client, auth, member["id"])
        if c["cohort"] == 1
    ] == [
        (b["id"], 1),
        (a["id"], 2),
    ]

    # A new number is a new person: invitation starts over and they get an SMS.
    new_phone = random_phone()
    changed = await client.patch(f"{url}/{a['id']}", json={"phone": new_phone}, headers=auth)
    assert changed.status_code == 200 and changed.json()["invitation_status"] == "pending"
    assert any(m.to == new_phone for m in app.state.providers.sms.sent)
    ah_phone = await client.patch(f"{url}/{ah['id']}", json={"phone": random_phone()}, headers=auth)
    assert ah_phone.status_code == 409 and ah_phone.json()["code"] == "account_holder_contact"
    missing = await client.patch(f"{url}/{uuid.uuid4()}", json={"name": "x"}, headers=auth)
    assert missing.status_code == 404 and missing.json()["code"] == "not_found"


async def test_self_member_cannot_list_own_number(client: AsyncClient, app: FastAPI) -> None:
    auth = await account_holder(client, app)
    phone = (await client.get("/me", headers=auth)).json()["phone"]
    me_member = await add_member(client, auth, name="Me", relationship="self", age=40)
    own = await add_contact(client, auth, me_member["id"], phone=phone)
    assert own.status_code == 400 and own.json()["code"] == "own_number"


async def test_copy_contacts(client: AsyncClient, app: FastAPI) -> None:
    auth = await account_holder(client, app)
    child = await add_member(client, auth)
    me_member = await add_member(client, auth, name="Me", relationship="self", age=40)
    a = (await add_contact(client, auth, child["id"], name="Meera")).json()
    await client.patch(
        f"/family/members/{child['id']}/contacts/{a['id']}",
        json={"is_guardian": True},
        headers=auth,
    )
    b = (await add_contact(client, auth, child["id"], cohort=2, name="Ravi")).json()

    copied = await client.post(
        f"/family/members/{me_member['id']}/contacts/copy",
        json={"from_member_id": child["id"]},
        headers=auth,
    )
    assert copied.status_code == 200, copied.text
    result = copied.json()
    # The account holder's own entry is not copied onto their own band.
    assert [(c["phone"], c["cohort"], c["is_guardian"]) for c in result] == [
        (a["phone"], 1, True),
        (b["phone"], 2, False),
    ]
    assert all(c["invitation_status"] == "pending" for c in result)

    again = await client.post(
        f"/family/members/{me_member['id']}/contacts/copy",
        json={"from_member_id": child["id"]},
        headers=auth,
    )
    assert len(again.json()) == 2  # nothing duplicated
    same = await client.post(
        f"/family/members/{child['id']}/contacts/copy",
        json={"from_member_id": child["id"]},
        headers=auth,
    )
    assert same.status_code == 400 and same.json()["code"] == "same_member"


async def test_ready_needs_band_cohort1_and_guardian(client: AsyncClient, app: FastAPI) -> None:
    auth = await account_holder(client, app)
    me_member = await add_member(client, auth, name="Me", relationship="self", age=40)
    band = await seed_band(app)
    claimed = await client.post(
        "/devices/claim",
        json={"device_id": band["band_id"], "member_id": me_member["id"]},
        headers=auth,
    )
    assert claimed.json()["missing"] == ["cohort1_contact", "guardian"]

    c2 = (await add_contact(client, auth, me_member["id"], cohort=2)).json()
    await client.patch(
        f"/family/members/{me_member['id']}/contacts/{c2['id']}",
        json={"is_guardian": True},
        headers=auth,
    )
    [m] = [
        x
        for x in (await client.get("/family/members", headers=auth)).json()
        if x["id"] == me_member["id"]
    ]
    assert m["ready"] is False and m["missing"] == ["cohort1_contact"]

    await add_contact(client, auth, me_member["id"], cohort=1)
    [m] = [
        x
        for x in (await client.get("/family/members", headers=auth)).json()
        if x["id"] == me_member["id"]
    ]
    assert m["ready"] is True and m["missing"] == []


async def test_database_enforces_limits_without_the_service(
    client: AsyncClient, app: FastAPI, session: AsyncSession
) -> None:
    auth = await account_holder(client, app)
    member = await add_member(client, auth)
    for _ in range(2):
        await add_contact(client, auth, member["id"])
    wearer_id = uuid.UUID(member["id"])
    with pytest.raises(IntegrityError, match="contacts_guardian_limit"):
        await session.execute(
            update(Contact).where(Contact.wearer_id == wearer_id).values(is_guardian=True)
        )
    await session.rollback()

    for i in range(3, 6):
        session.add(
            Contact(
                wearer_id=wearer_id,
                name="x",
                phone=random_phone(),
                relationship="x",
                cohort=1,
                priority=i + 10,
                proximity="low",
                availability="available",
            )
        )
    with pytest.raises(IntegrityError, match="contacts_cohort_full"):
        await session.flush()
