"""/me: the signed-in user."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, status
from pydantic import AfterValidator, BaseModel, Field, StringConstraints

from app.api.deps import ClientDep, CurrentUser, SessionDep
from app.api.types import Gender, Text100
from app.core.errors import error_responses
from app.db.models import User
from app.services import me as me_service

router = APIRouter(prefix="/me", tags=["me"], responses=error_responses(401))

BirthDate = Annotated[date | None, AfterValidator(me_service.validate_birth_date)]


class MeResponse(BaseModel):
    id: uuid.UUID
    phone: str
    name: str | None
    onboarding_state: me_service.OnboardingState
    watching_count: int
    pending_invitation_count: int
    family_member_count: int


class RoleRequest(BaseModel):
    role: me_service.Role


class ProfileResponse(BaseModel):
    phone: str
    name: str | None
    date_of_birth: date | None
    gender: Gender | None
    city: str | None


class ProfileUpdate(BaseModel):
    """Only the fields sent are changed; `null` clears one. The phone can't change."""

    name: Text100 | None = None
    date_of_birth: BirthDate = None
    gender: Gender | None = None
    city: Text100 | None = None


class NotificationSettingsBody(BaseModel):
    emergency_alerts: bool = Field(
        default=True, description="Always true; emergency alerts cannot be turned off."
    )
    band_offline: bool
    low_battery: bool
    test_reminders: bool
    firmware_updates: bool


class PushTokenRequest(BaseModel):
    token: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    platform: Literal["android", "ios"]


def _profile(user: User) -> ProfileResponse:
    return ProfileResponse(
        phone=user.phone,
        name=user.name,
        date_of_birth=user.date_of_birth,
        gender=user.gender,  # DB check constraint keeps it one of Gender
        city=user.city,
    )


@router.get("", response_model=MeResponse, summary="Who am I, and where in onboarding")
async def get_me(user: CurrentUser, session: SessionDep) -> MeResponse:
    counts = await me_service.counts_for(session, user)
    return MeResponse(
        id=user.id,
        phone=user.phone,
        name=user.name,
        onboarding_state=me_service.onboarding_state(user, counts),
        watching_count=counts.watching_count,
        pending_invitation_count=counts.pending_invitation_count,
        family_member_count=counts.family_member_count,
    )


@router.post(
    "/role",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Role screen: set up my band, or someone added me as a contact",
)
async def set_role(
    body: RoleRequest, user: CurrentUser, session: SessionDep, client: ClientDep
) -> None:
    await me_service.set_role(session, user, client, body.role)


@router.get("/profile", response_model=ProfileResponse)
async def get_profile(user: CurrentUser) -> ProfileResponse:
    return _profile(user)


@router.patch("/profile", response_model=ProfileResponse)
async def update_profile(
    body: ProfileUpdate, user: CurrentUser, session: SessionDep, client: ClientDep
) -> ProfileResponse:
    changes = body.model_dump(include=body.model_fields_set)
    updated = await me_service.update_profile(session, user, client, changes)
    return _profile(updated)


@router.get("/notification-settings", response_model=NotificationSettingsBody)
async def get_notification_settings(
    user: CurrentUser, session: SessionDep
) -> NotificationSettingsBody:
    prefs = await me_service.get_notification_settings(session, user)
    return NotificationSettingsBody(
        emergency_alerts=True,
        band_offline=prefs.band_offline,
        low_battery=prefs.low_battery,
        test_reminders=prefs.test_reminders,
        firmware_updates=prefs.firmware_updates,
    )


@router.put("/notification-settings", status_code=status.HTTP_204_NO_CONTENT)
async def save_notification_settings(
    body: NotificationSettingsBody, user: CurrentUser, session: SessionDep, client: ClientDep
) -> None:
    # emergency_alerts is ignored: it is always on.
    await me_service.save_notification_settings(
        session,
        user,
        client,
        me_service.NotificationSettings(
            band_offline=body.band_offline,
            low_battery=body.low_battery,
            test_reminders=body.test_reminders,
            firmware_updates=body.firmware_updates,
        ),
    )


@router.post(
    "/push-tokens",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Register this phone's FCM token (after login and whenever it rotates)",
)
async def register_push_token(
    body: PushTokenRequest, user: CurrentUser, session: SessionDep
) -> None:
    await me_service.register_push_token(session, user, token=body.token, platform=body.platform)
