"""/family/members: family members (wearers) and their emergency contacts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile, status
from pydantic import BaseModel, Field

from app.api.deps import (
    ClientDep,
    CurrentUser,
    PhotoUrl,
    PhotoUrlDep,
    ProvidersDep,
    SessionDep,
    SettingsDep,
)
from app.api.types import (
    Availability,
    Cohort,
    Gender,
    InvitationStatus,
    OptionalText100,
    Proximity,
    Relationship,
    Signal,
    Text50,
    Text100,
)
from app.core.config import Settings
from app.core.errors import AppError, error_responses
from app.core.phone import IndianPhone
from app.db.models import Contact, Device
from app.services import contacts as contacts_service
from app.services import devices as devices_service
from app.services import family as family_service
from app.services.family import Missing

router = APIRouter(prefix="/family/members", tags=["family"], responses=error_responses(401))


# ---------------------------------------------------------------------------- models


class BandOut(BaseModel):
    id: str = Field(description="Band ID printed on the band, e.g. KD-0001")
    battery_percent: int | None = Field(description="Null until the band has reported it")
    is_connected: bool
    last_seen_at: datetime | None
    signal: Signal | None
    firmware_version: str | None


class MemberOut(BaseModel):
    id: uuid.UUID
    name: str
    relationship: Relationship
    age: int
    gender: Gender
    photo_url: str | None = Field(description="Signed link, valid for a limited time")
    consent_at: datetime
    band: BandOut | None
    ready: bool = Field(description="Band claimed, a Cohort 1 contact and a guardian")
    missing: list[Missing] = Field(description="What stops the band being ready")


class RenameIn(BaseModel):
    name: Text100


class ContactIn(BaseModel):
    name: Text100
    phone: IndianPhone
    relationship: Text50
    city: OptionalText100 = ""
    proximity: Proximity = "moderate"
    availability: Availability = "available"
    cohort: Cohort


class ContactPatch(BaseModel):
    """Only the fields sent are changed."""

    name: Text100 | None = None
    phone: IndianPhone | None = None
    relationship: Text50 | None = None
    city: OptionalText100 | None = None
    proximity: Proximity | None = None
    availability: Availability | None = None
    cohort: Cohort | None = None
    is_guardian: bool | None = None


class ContactOut(BaseModel):
    id: uuid.UUID
    name: str
    phone: str
    relationship: str
    city: str
    cohort: Cohort
    priority: int = Field(description="Call order within the cohort, from 1")
    proximity: Proximity
    availability: Availability
    is_guardian: bool
    is_account_holder: bool
    invitation_status: InvitationStatus


class ReorderIn(BaseModel):
    cohort: Cohort
    ids: Annotated[list[uuid.UUID], Field(max_length=5)]


class CopyIn(BaseModel):
    from_member_id: uuid.UUID


# ---------------------------------------------------------------------------- serialisers


def band_out(settings: Settings, device: Device | None) -> BandOut | None:
    if device is None:
        return None
    return BandOut(
        id=device.band_id,
        battery_percent=device.battery_percent,
        is_connected=devices_service.is_connected(settings, device),
        last_seen_at=device.last_seen_at,
        signal=device.signal,  # DB check constraint
        firmware_version=device.firmware_version,
    )


def member_out(
    settings: Settings, photo_url: PhotoUrl, view: family_service.MemberView
) -> MemberOut:
    w = view.wearer
    return MemberOut(
        id=w.id,
        name=w.name,
        relationship=w.relationship,  # DB check constraint
        age=w.age,
        gender=w.gender,
        photo_url=photo_url(w.photo_key),
        consent_at=w.consent_at,
        band=band_out(settings, view.device),
        ready=view.readiness.ready,
        missing=view.readiness.missing,
    )


def contact_out(c: Contact) -> ContactOut:
    return ContactOut.model_validate(c, from_attributes=True)


# ---------------------------------------------------------------------------- members


@router.get("", response_model=list[MemberOut], summary="Family members with their bands")
async def list_members(
    user: CurrentUser, session: SessionDep, settings: SettingsDep, photo_url: PhotoUrlDep
) -> list[MemberOut]:
    views = await family_service.list_members(session, user)
    return [member_out(settings, photo_url, v) for v in views]


@router.post(
    "",
    response_model=MemberOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a family member (multipart form; consent must be true)",
    responses=error_responses(400, 409, 413),
)
async def create_member(
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    providers: ProvidersDep,
    client: ClientDep,
    photo_url: PhotoUrlDep,
    name: Annotated[str, Form(min_length=1, max_length=100)],
    relationship: Annotated[Relationship, Form()],
    age: Annotated[int, Form(ge=0, le=120)],
    gender: Annotated[Gender, Form()],
    consent: Annotated[bool, Form(description="The consent statement was agreed to")],
    photo: Annotated[UploadFile | None, File(description="JPEG, PNG, WebP or HEIC")] = None,
) -> MemberOut:
    picture = None
    if photo is not None and photo.filename:
        data = await photo.read(settings.photo_max_bytes + 1)
        if len(data) > settings.photo_max_bytes:
            raise AppError("photo_too_large", 413, max_bytes=settings.photo_max_bytes)
        content_type = family_service.sniff_photo(data)
        if content_type is None:
            raise AppError("invalid_photo", 400)
        picture = family_service.Photo(data=data, content_type=content_type)
    clean_name = name.strip()
    if not clean_name:
        raise AppError("invalid_name", 400)
    view = await family_service.create_member(
        session,
        providers.storage,
        user,
        client,
        name=clean_name,
        relationship=relationship,
        age=age,
        gender=gender,
        consent=consent,
        photo=picture,
    )
    return member_out(settings, photo_url, view)


@router.patch("/{member_id}", response_model=MemberOut, responses=error_responses(404))
async def rename_member(
    member_id: uuid.UUID,
    body: RenameIn,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    client: ClientDep,
    photo_url: PhotoUrlDep,
) -> MemberOut:
    view = await family_service.rename_member(session, user, client, member_id, body.name)
    return member_out(settings, photo_url, view)


# ---------------------------------------------------------------------------- contacts

_contact_errors = error_responses(400, 404, 409)


@router.get(
    "/{member_id}/contacts",
    response_model=list[ContactOut],
    summary="Cohort 1 then 2, in call order",
    responses=error_responses(404),
)
async def list_contacts(
    member_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> list[ContactOut]:
    return [contact_out(c) for c in await contacts_service.list_contacts(session, user, member_id)]


@router.post(
    "/{member_id}/contacts",
    response_model=ContactOut,
    status_code=status.HTTP_201_CREATED,
    responses=_contact_errors,
)
async def add_contact(
    member_id: uuid.UUID,
    body: ContactIn,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    providers: ProvidersDep,
    client: ClientDep,
) -> ContactOut:
    contact = await contacts_service.add_contact(
        session,
        settings,
        providers.sms,
        user,
        client,
        member_id,
        contacts_service.ContactInput(**body.model_dump()),
    )
    return contact_out(contact)


# Declared before /{contact_id} so "reorder" is not read as a contact id.
@router.patch(
    "/{member_id}/contacts/reorder",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="New call order of one cohort (all of its contact ids)",
    responses=_contact_errors,
)
async def reorder_contacts(
    member_id: uuid.UUID, body: ReorderIn, user: CurrentUser, session: SessionDep, client: ClientDep
) -> None:
    await contacts_service.reorder(session, user, client, member_id, body.cohort, body.ids)


@router.post(
    "/{member_id}/contacts/copy",
    response_model=list[ContactOut],
    summary="Copy another member's contacts; returns this member's full list",
    responses=_contact_errors,
)
async def copy_contacts(
    member_id: uuid.UUID,
    body: CopyIn,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    providers: ProvidersDep,
    client: ClientDep,
) -> list[ContactOut]:
    result = await contacts_service.copy_contacts(
        session,
        settings,
        providers.sms,
        user,
        client,
        from_member_id=body.from_member_id,
        to_member_id=member_id,
    )
    return [contact_out(c) for c in result]


@router.patch(
    "/{member_id}/contacts/{contact_id}",
    response_model=ContactOut,
    summary="Edit, move cohort, make / unmake guardian",
    responses=_contact_errors,
)
async def update_contact(
    member_id: uuid.UUID,
    contact_id: uuid.UUID,
    body: ContactPatch,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    providers: ProvidersDep,
    client: ClientDep,
) -> ContactOut:
    # `null` for a field means "leave as is" (none of them can be cleared).
    changes = {
        k: v for k, v in body.model_dump(include=body.model_fields_set).items() if v is not None
    }
    contact = await contacts_service.update_contact(
        session, settings, providers.sms, user, client, member_id, contact_id, changes
    )
    return contact_out(contact)


@router.delete(
    "/{member_id}/contacts/{contact_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_contact_errors,
)
async def remove_contact(
    member_id: uuid.UUID,
    contact_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    client: ClientDep,
) -> None:
    await contacts_service.remove_contact(session, user, client, member_id, contact_id)
