"""/devices: pairing a band from the app (availability, vibrate check, claim,
deactivate). The band's own API is /device/v1 (device_api.py)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, status
from pydantic import BaseModel, Field

from app.api.deps import (
    ClientDep,
    CurrentUser,
    PhotoUrlDep,
    ProvidersDep,
    RedisDep,
    SessionDep,
    SettingsDep,
)
from app.api.family import MemberOut, member_out
from app.api.types import BandId
from app.core.errors import error_responses
from app.services import devices as devices_service

router = APIRouter(prefix="/devices", tags=["devices"], responses=error_responses(401))

BandIdPath = Annotated[str, Path(min_length=6, max_length=32, pattern=r"^[A-Za-z0-9-]+$")]
_lookup_errors = error_responses(404, 409, 429)


class AvailabilityOut(BaseModel):
    battery_percent: int | None
    firmware_version: str | None
    firmware_up_to_date: bool | None


class VibrateCheckOut(BaseModel):
    pairing_id: uuid.UUID
    expires_in: int = Field(description="Seconds the band has to answer")


class ConfirmIn(BaseModel):
    felt: bool = Field(description='Answer to "Did you feel the pulse?"')


class ConfirmOut(BaseModel):
    band_acked: bool = Field(description="The band reported back that it received the SMS")


class ClaimIn(BaseModel):
    device_id: BandId
    member_id: uuid.UUID


@router.get(
    "/{band_id}/availability",
    response_model=AvailabilityOut,
    summary="Can this band be paired? 404 not_found, 409 not_sold / already_active (+linked_at)",
    responses=_lookup_errors,
)
async def availability(
    band_id: BandIdPath,
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
) -> AvailabilityOut:
    device = await devices_service.availability(session, redis, settings, user, band_id)
    return AvailabilityOut(
        battery_percent=device.battery_percent,
        firmware_version=device.firmware_version,
        firmware_up_to_date=devices_service.firmware_up_to_date(settings, device),
    )


@router.post(
    "/{band_id}/vibrate-check",
    response_model=VibrateCheckOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Text the band `KD1 PAIR <nonce>` so it pulses (QR / typed code path)",
    responses={**_lookup_errors, **error_responses(503)},
)
async def vibrate_check(
    band_id: BandIdPath,
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
    providers: ProvidersDep,
    client: ClientDep,
) -> VibrateCheckOut:
    check = await devices_service.vibrate_check(
        session, redis, settings, providers.sms, user, client, band_id
    )
    return VibrateCheckOut(pairing_id=check.pairing_id, expires_in=check.expires_in)


@router.post(
    "/{band_id}/vibrate-check/confirm",
    response_model=ConfirmOut,
    summary="Record whether the user felt the pulse (latest vibrate check)",
    responses=error_responses(404, 409),
)
async def confirm_vibration(
    band_id: BandIdPath,
    body: ConfirmIn,
    user: CurrentUser,
    session: SessionDep,
    client: ClientDep,
) -> ConfirmOut:
    pairing = await devices_service.confirm_vibration(
        session, user, client, band_id, felt=body.felt
    )
    return ConfirmOut(band_acked=pairing.acked_at is not None)


@router.post(
    "/claim",
    response_model=MemberOut,
    summary="Link the band to a family member; returns the member with their band",
    responses=error_responses(404, 409),
)
async def claim(
    body: ClaimIn,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    client: ClientDep,
    photo_url: PhotoUrlDep,
) -> MemberOut:
    view = await devices_service.claim(
        session, settings, user, client, band_id=body.device_id, member_id=body.member_id
    )
    return member_out(settings, photo_url, view)


@router.post(
    "/{band_id}/deactivate",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Release the band from its member (it can be paired again)",
    responses=error_responses(404),
)
async def deactivate(
    band_id: BandIdPath, user: CurrentUser, session: SessionDep, client: ClientDep
) -> None:
    await devices_service.deactivate(session, user, client, band_id)
