"""/family/members: add with consent + photo, the account holder auto-contact,
rename, isolation between accounts, /me counts."""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, Wearer
from app.services.family import CONSENT_SHA256, CONSENT_VERSION
from tests.helpers import PNG, account_holder, add_member


async def test_add_member_with_consent_and_photo(
    client: AsyncClient, app: FastAPI, session: AsyncSession
) -> None:
    auth = await account_holder(client, app)
    member = await add_member(client, auth, photo=PNG)

    assert member["name"] == "Asha" and member["relationship"] == "daughter"
    assert member["band"] is None and member["ready"] is False
    # The account holder is already a Cohort 1 guardian, so only the band is missing.
    assert member["missing"] == ["band"]
    assert member["consent_at"]

    # The photo is behind a signed, expiring link.
    photo = await client.get(member["photo_url"])
    assert photo.status_code == 200 and photo.content == PNG
    assert photo.headers["content-type"] == "image/png"
    tampered = member["photo_url"].replace("sig=", "sig=0")
    assert (await client.get(tampered)).status_code == 404

    wearer = await session.get(Wearer, uuid.UUID(member["id"]))
    assert wearer is not None and wearer.consent_version == CONSENT_VERSION
    audit = await session.scalar(
        select(AuditLog).where(
            AuditLog.action == "member.created", AuditLog.entity_id == member["id"]
        )
    )
    assert audit is not None and audit.details["consent_text_sha256"] == CONSENT_SHA256
    assert "Asha" not in str(audit.details)  # names are not written to the audit trail


async def test_account_holder_is_cohort1_guardian_for_family_not_self(
    client: AsyncClient, app: FastAPI
) -> None:
    auth = await account_holder(client, app)
    me = (await client.get("/me", headers=auth)).json()
    child = await add_member(client, auth)
    me_member = await add_member(client, auth, name="Me", relationship="self", age=38)

    [ah] = (await client.get(f"/family/members/{child['id']}/contacts", headers=auth)).json()
    assert ah["phone"] == me["phone"] and ah["cohort"] == 1 and ah["priority"] == 1
    assert ah["is_guardian"] and ah["is_account_holder"] and ah["invitation_status"] == "accepted"
    assert ah["relationship"] == "parent"

    own = await client.get(f"/family/members/{me_member['id']}/contacts", headers=auth)
    assert own.json() == []
    assert me_member["missing"] == ["band", "cohort1_contact", "guardian"]

    second_self = await client.post(
        "/family/members",
        data={
            "name": "Me 2",
            "relationship": "self",
            "age": "38",
            "gender": "female",
            "consent": "true",
        },
        headers=auth,
    )
    assert second_self.status_code == 409 and second_self.json()["code"] == "self_exists"

    listed = (await client.get("/family/members", headers=auth)).json()
    assert [m["relationship"] for m in listed] == ["self", "daughter"]  # self first


async def test_add_member_rejects_bad_input(client: AsyncClient, app: FastAPI) -> None:
    auth = await account_holder(client, app)
    base = {"name": "Asha", "relationship": "daughter", "age": "14", "gender": "female"}

    no_consent = await client.post(
        "/family/members", data={**base, "consent": "false"}, headers=auth
    )
    assert no_consent.status_code == 400 and no_consent.json()["code"] == "consent_required"

    bad_photo = await client.post(
        "/family/members",
        data={**base, "consent": "true"},
        files={"photo": ("x.png", b"not an image", "image/png")},
        headers=auth,
    )
    assert bad_photo.status_code == 400 and bad_photo.json()["code"] == "invalid_photo"

    for field, value in (("relationship", "cousin"), ("age", "200"), ("gender", "x")):
        response = await client.post(
            "/family/members", data={**base, field: value, "consent": "true"}, headers=auth
        )
        assert response.status_code == 422, field


async def test_rename_and_other_accounts_cannot_see_members(
    client: AsyncClient, app: FastAPI, session: AsyncSession
) -> None:
    auth = await account_holder(client, app)
    other = await account_holder(client, app)
    member = await add_member(client, auth)

    renamed = await client.patch(
        f"/family/members/{member['id']}", json={"name": "  Asha B  "}, headers=auth
    )
    assert renamed.status_code == 200 and renamed.json()["name"] == "Asha B"
    assert await session.scalar(
        select(AuditLog).where(
            AuditLog.action == "member.renamed", AuditLog.entity_id == member["id"]
        )
    )

    assert (await client.get("/family/members", headers=other)).json() == []
    for response in (
        await client.patch(f"/family/members/{member['id']}", json={"name": "x"}, headers=other),
        await client.get(f"/family/members/{member['id']}/contacts", headers=other),
    ):
        assert response.status_code == 404 and response.json()["code"] == "not_found"


async def test_me_counts_move_onboarding_to_home(client: AsyncClient, app: FastAPI) -> None:
    auth = await account_holder(client, app)
    assert (await client.get("/me", headers=auth)).json()[
        "onboarding_state"
    ] == "needs_first_member"
    await add_member(client, auth)
    me = (await client.get("/me", headers=auth)).json()
    assert me["family_member_count"] == 1 and me["onboarding_state"] == "home"
