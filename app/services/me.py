"""The signed-in user: onboarding state, role, profile, notification settings,
push tokens."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal

from sqlalchemy import Result, func, literal_column, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client import ClientInfo
from app.core.logging import get_logger
from app.db.models import NotificationPreferences, PushToken, User, Wearer
from app.services import invitations
from app.services.audit import record_audit

log = get_logger("kaladhaga.me")

OnboardingState = Literal["needs_role", "needs_first_member", "home", "watching"]
Role = Literal["account_holder", "contact"]


@dataclass(frozen=True, slots=True)
class MeCounts:
    family_member_count: int = 0
    watching_count: int = 0
    pending_invitation_count: int = 0


async def counts_for(session: AsyncSession, user: User) -> MeCounts:
    members = await session.scalar(
        select(func.count()).select_from(Wearer).where(Wearer.account_id == user.id)
    )
    watching, pending = await invitations.counts(session, user)
    return MeCounts(
        family_member_count=int(members or 0),
        watching_count=watching,
        pending_invitation_count=pending,
    )


def onboarding_state(user: User, counts: MeCounts) -> OnboardingState:
    if user.role is None:
        return "needs_role"
    if user.role == "contact":
        return "watching"
    return "home" if counts.family_member_count > 0 else "needs_first_member"


async def set_role(session: AsyncSession, user: User, client: ClientInfo, role: Role) -> None:
    previous = user.role
    user.role = role
    record_audit(
        session,
        action="me.role_set",
        actor_type="user",
        actor_id=str(user.id),
        entity_type="user",
        entity_id=str(user.id),
        ip=client.ip,
        details={"from": previous, "to": role},
    )
    await session.commit()
    log.info("me.role_set", previous=previous, role=role)


async def update_profile(
    session: AsyncSession, user: User, client: ClientInfo, changes: dict[str, Any]
) -> User:
    changed = sorted(k for k, v in changes.items() if getattr(user, k) != v)
    for key in changed:
        setattr(user, key, changes[key])
    if changed:
        # Field names only: the values are personal data.
        record_audit(
            session,
            action="me.profile_updated",
            actor_type="user",
            actor_id=str(user.id),
            entity_type="user",
            entity_id=str(user.id),
            ip=client.ip,
            details={"fields": changed},
        )
        await session.commit()
        await session.refresh(user)
    log.info("me.profile_updated", fields=changed)
    return user


@dataclass(frozen=True, slots=True)
class NotificationSettings:
    band_offline: bool = True
    low_battery: bool = True
    test_reminders: bool = True
    firmware_updates: bool = True


async def get_notification_settings(session: AsyncSession, user: User) -> NotificationSettings:
    row = await session.get(NotificationPreferences, user.id)
    if row is None:
        return NotificationSettings()
    return NotificationSettings(
        band_offline=row.band_offline,
        low_battery=row.low_battery,
        test_reminders=row.test_reminders,
        firmware_updates=row.firmware_updates,
    )


async def save_notification_settings(
    session: AsyncSession, user: User, client: ClientInfo, prefs: NotificationSettings
) -> None:
    values = {
        "band_offline": prefs.band_offline,
        "low_battery": prefs.low_battery,
        "test_reminders": prefs.test_reminders,
        "firmware_updates": prefs.firmware_updates,
    }
    stmt = insert(NotificationPreferences).values(user_id=user.id, **values)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[NotificationPreferences.user_id],
            set_={**values, "updated_at": datetime.now(UTC)},
        )
    )
    record_audit(
        session,
        action="me.notification_settings_updated",
        actor_type="user",
        actor_id=str(user.id),
        entity_type="user",
        entity_id=str(user.id),
        ip=client.ip,
        details=values,
    )
    await session.commit()
    log.info("me.notification_settings_updated", **values)


async def register_push_token(
    session: AsyncSession, user: User, *, token: str, platform: Literal["android", "ios"]
) -> None:
    """Upsert: a token always belongs to the user who registered it last."""
    now = datetime.now(UTC)
    stmt = insert(PushToken).values(user_id=user.id, token=token, platform=platform)
    result: Result[Any] = await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[PushToken.token],
            set_={"user_id": user.id, "platform": platform, "last_seen_at": now},
        ).returning(literal_column("xmax = 0"))  # true when the row was inserted
    )
    is_new = bool(result.scalar_one())
    await session.commit()
    # A brand-new token is logged at INFO; refreshes of a known one at DEBUG.
    (log.info if is_new else log.debug)("me.push_token_registered", platform=platform, new=is_new)


def validate_birth_date(value: date | None) -> date | None:
    if value is None:
        return None
    today = datetime.now(UTC).date()
    if value > today or value.year < 1900:
        raise ValueError("date_of_birth must be between 1900 and today")
    return value
