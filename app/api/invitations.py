"""/me/invites and /me/watching: the contact side."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from app.api.deps import ClientDep, CurrentUser, PhotoUrlDep, SessionDep, SettingsDep
from app.api.family import BandOut, band_out
from app.api.types import Cohort, Gender
from app.core.errors import error_responses
from app.services import invitations as invitations_service

router = APIRouter(prefix="/me", tags=["invitations"], responses=error_responses(401))


class InviteOut(BaseModel):
    id: uuid.UUID = Field(description="Invitation id (use with accept / decline)")
    member_id: uuid.UUID
    member_name: str
    account_holder_name: str | None
    account_holder_phone: str
    relationship: str = Field(description="How the account holder described you")
    cohort: Cohort
    is_guardian: bool
    invited_at: datetime | None


class WatchedOut(BaseModel):
    contact_id: uuid.UUID
    member_id: uuid.UUID
    member_name: str
    age: int
    gender: Gender
    photo_url: str | None
    account_holder_name: str | None
    relationship: str
    cohort: Cohort
    is_guardian: bool
    band: BandOut | None


@router.get("/invites", response_model=list[InviteOut], summary="Invitations for this phone")
async def list_invites(user: CurrentUser, session: SessionDep) -> list[InviteOut]:
    return [
        InviteOut(
            id=i.contact.id,
            member_id=i.wearer.id,
            member_name=i.wearer.name,
            account_holder_name=i.account_holder.name,
            account_holder_phone=i.account_holder.phone,
            relationship=i.contact.relationship,
            cohort=i.contact.cohort,  # DB check constraint
            is_guardian=i.contact.is_guardian,
            invited_at=i.contact.invited_at,
        )
        for i in await invitations_service.list_invites(session, user)
    ]


@router.post(
    "/invites/{invite_id}/accept",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Accept; also sets role to contact if none was chosen yet",
    responses=error_responses(404),
)
async def accept_invite(
    invite_id: uuid.UUID, user: CurrentUser, session: SessionDep, client: ClientDep
) -> None:
    await invitations_service.accept(session, user, client, invite_id)


@router.post(
    "/invites/{invite_id}/decline",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(404, 409),
)
async def decline_invite(
    invite_id: uuid.UUID, user: CurrentUser, session: SessionDep, client: ClientDep
) -> None:
    await invitations_service.decline(session, user, client, invite_id)


@router.get(
    "/watching", response_model=list[WatchedOut], summary="People I protect (accepted invitations)"
)
async def list_watching(
    user: CurrentUser, session: SessionDep, settings: SettingsDep, photo_url: PhotoUrlDep
) -> list[WatchedOut]:
    return [
        WatchedOut(
            contact_id=w.contact.id,
            member_id=w.wearer.id,
            member_name=w.wearer.name,
            age=w.wearer.age,
            gender=w.wearer.gender,
            photo_url=photo_url(w.wearer.photo_key),
            account_holder_name=w.account_holder.name,
            relationship=w.contact.relationship,
            cohort=w.contact.cohort,
            is_guardian=w.contact.is_guardian,
            band=band_out(settings, w.device),
        )
        for w in await invitations_service.list_watching(session, user)
    ]
