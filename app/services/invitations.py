"""The contact side: invitations waiting for this phone, accept / decline, and
the members this user protects ("watching").

An invitation is a `contacts` row whose phone is this user's phone. Accepting
links the row to the user (`contact_user_id`). The account holder's own entries
are never invitations.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client import ClientInfo
from app.core.errors import AppError
from app.core.logging import get_logger
from app.db.models import Contact, Device, User, Wearer
from app.services import family
from app.services.audit import record_audit

log = get_logger("kaladhaga.invitations")


@dataclass(frozen=True, slots=True)
class Invite:
    contact: Contact
    wearer: Wearer
    account_holder: User


@dataclass(frozen=True, slots=True)
class Watched:
    contact: Contact
    wearer: Wearer
    account_holder: User
    device: Device | None


def _invites_for(user: User) -> tuple[ColumnElement[bool], ...]:
    return (
        Contact.phone == user.phone,
        Contact.invitation_status == "pending",
        Contact.is_account_holder.is_(False),
    )


def _watching_for(user: User) -> tuple[ColumnElement[bool], ...]:
    return (
        Contact.contact_user_id == user.id,
        Contact.invitation_status == "accepted",
        Contact.is_account_holder.is_(False),
    )


async def counts(session: AsyncSession, user: User) -> tuple[int, int]:
    """(watching, pending invitations)"""
    watching = await session.scalar(select(func.count()).where(*_watching_for(user)))
    pending = await session.scalar(select(func.count()).where(*_invites_for(user)))
    return int(watching or 0), int(pending or 0)


async def list_invites(session: AsyncSession, user: User) -> list[Invite]:
    rows = await session.execute(
        select(Contact, Wearer, User)
        .join(Wearer, Wearer.id == Contact.wearer_id)
        .join(User, User.id == Wearer.account_id)
        .where(*_invites_for(user))
        .order_by(Contact.created_at.desc())
    )
    return [Invite(c, w, ah) for c, w, ah in rows]


async def list_watching(session: AsyncSession, user: User) -> list[Watched]:
    rows = (
        await session.execute(
            select(Contact, Wearer, User)
            .join(Wearer, Wearer.id == Contact.wearer_id)
            .join(User, User.id == Wearer.account_id)
            .where(*_watching_for(user))
            .order_by(Contact.accepted_at, Contact.id)
        )
    ).all()
    devices = await family.claimed_devices(session, [w.id for _, w, _ in rows])
    return [Watched(c, w, ah, devices.get(w.id)) for c, w, ah in rows]


async def _invite(session: AsyncSession, user: User, invite_id: uuid.UUID) -> Contact:
    contact: Contact | None = await session.scalar(
        select(Contact)
        .where(
            Contact.id == invite_id,
            Contact.phone == user.phone,
            Contact.is_account_holder.is_(False),
        )
        .with_for_update()
    )
    if contact is None:
        log.info("invite.not_found", invite_id=str(invite_id))
        raise AppError("not_found", 404)
    return contact


async def accept(
    session: AsyncSession, user: User, client: ClientInfo, invite_id: uuid.UUID
) -> None:
    contact = await _invite(session, user, invite_id)
    if contact.invitation_status == "accepted" and contact.contact_user_id == user.id:
        return  # already accepted: nothing to do
    previous = contact.invitation_status
    contact.invitation_status = "accepted"
    contact.contact_user_id = user.id
    contact.accepted_at = datetime.now(UTC)
    contact.declined_at = None
    # Someone who accepts before choosing a role is a contact (skips the role screen).
    role_set = user.role is None
    if role_set:
        user.role = "contact"
    record_audit(
        session,
        action="invite.accepted",
        actor_type="user",
        actor_id=str(user.id),
        entity_type="contact",
        entity_id=str(contact.id),
        ip=client.ip,
        details={
            "member_id": str(contact.wearer_id),
            "previous_status": previous,
            "is_guardian": contact.is_guardian,
            "role_set_to_contact": role_set,
        },
    )
    await session.commit()
    log.info("invite.accepted", contact_id=str(contact.id), is_guardian=contact.is_guardian)


async def decline(
    session: AsyncSession, user: User, client: ClientInfo, invite_id: uuid.UUID
) -> None:
    contact = await _invite(session, user, invite_id)
    if contact.invitation_status == "declined":
        return
    if contact.invitation_status == "accepted":
        raise AppError("already_accepted", 409)
    contact.invitation_status = "declined"
    contact.declined_at = datetime.now(UTC)
    record_audit(
        session,
        action="invite.declined",
        actor_type="user",
        actor_id=str(user.id),
        entity_type="contact",
        entity_id=str(contact.id),
        ip=client.ip,
        details={"member_id": str(contact.wearer_id), "is_guardian": contact.is_guardian},
    )
    await session.commit()
    log.info("invite.declined", contact_id=str(contact.id), is_guardian=contact.is_guardian)
