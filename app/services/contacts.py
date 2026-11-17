"""Emergency contacts of one member, and their invitations.

Rules (same as the app's ContactRules; the database trigger repeats the limits):
- Cohort 1 and Cohort 2, up to 5 each, in call order (`priority` 1..n, no gaps).
- Up to 2 guardians; once there is one, the last one can't be removed or unset.
- One entry per phone per member (`duplicate_phone`).
- A family member's Cohort 1 starts with the account holder as a guardian
  (`is_account_holder`, invitation already accepted). The account holder's own
  band ("self") starts blank, and they can't be their own contact.
- Guardians are set directly by the account holder; acceptance is not needed for
  the band to be ready. Everyone else gets an SMS invite and stays `pending`
  until they accept in the app.

Every change of a member's contacts locks that member row first, so concurrent
edits are applied one after another.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client import ClientInfo
from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.db.models import Contact, User, Wearer
from app.db.models.family import MAX_GUARDIANS, MAX_PER_COHORT
from app.providers.sms import SmsMessage, SmsProvider
from app.services.audit import record_audit
from app.services.family import owned_member

log = get_logger("kaladhaga.contacts")

# How the account holder is related to the member they added.
_ACCOUNT_HOLDER_RELATIONSHIP = {
    "son": "parent",
    "daughter": "parent",
    "wife": "spouse",
    "husband": "spouse",
    "mother": "child",
    "father": "child",
}

# Fields a client can set; audit rows list which ones changed (not their values).
EDITABLE = ("name", "phone", "relationship", "city", "proximity", "availability", "cohort")


@dataclass(frozen=True, slots=True)
class ContactInput:
    name: str
    phone: str
    relationship: str
    city: str
    proximity: str
    availability: str
    cohort: int


def _now() -> datetime:
    return datetime.now(UTC)


async def _contacts(session: AsyncSession, wearer_id: uuid.UUID) -> list[Contact]:
    rows = await session.scalars(
        select(Contact)
        .where(Contact.wearer_id == wearer_id)
        .order_by(Contact.cohort, Contact.priority, Contact.created_at)
    )
    return list(rows)


def _in_cohort(contacts: Sequence[Contact], cohort: int) -> list[Contact]:
    return sorted((c for c in contacts if c.cohort == cohort), key=lambda c: c.priority)


def _guardians(contacts: Sequence[Contact]) -> int:
    return sum(c.is_guardian for c in contacts)


def _compact(contacts: Sequence[Contact], cohort: int) -> None:
    """Renumber a cohort 1..n (the unique constraint is deferred to commit)."""
    for i, c in enumerate(_in_cohort(contacts, cohort), start=1):
        c.priority = i


async def _flush(session: AsyncSession) -> None:
    """Database limits as a last line of defence, mapped to the API codes."""
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        text = str(exc.orig)
        for marker, code in (
            ("uq_contacts_wearer_phone", "duplicate_phone"),
            ("contacts_cohort_full", "cohort_full"),
            ("contacts_guardian_limit", "guardian_limit"),
        ):
            if marker in text:
                log.warning("contacts.db_limit_hit", code=code)
                raise AppError(code, 409) from exc
        raise


def _audit(
    session: AsyncSession,
    user: User,
    client: ClientInfo,
    action: str,
    wearer: Wearer,
    contact_id: uuid.UUID | None,
    details: dict[str, Any],
) -> None:
    record_audit(
        session,
        action=action,
        actor_type="user",
        actor_id=str(user.id),
        entity_type="contact" if contact_id else "member",
        entity_id=str(contact_id or wearer.id),
        ip=client.ip,
        details={"member_id": str(wearer.id), **details},
    )


# ---------------------------------------------------------------------------- invitations


def _invite_text(settings: Settings, user: User, wearer: Wearer, contact: Contact) -> str:
    # TODO(exotel): must match the registered DLT template word for word.
    who = user.name or user.phone
    role = "an emergency contact and guardian" if contact.is_guardian else "an emergency contact"
    text = (
        f"{who} added you as {role} for {wearer.name} on Kala Dhaga. "
        "Install the Kala Dhaga app and sign in with this number to accept."
    )
    return f"{text} {settings.invite_link_url}" if settings.invite_link_url else text


async def send_invites(
    session: AsyncSession,
    settings: Settings,
    sms: SmsProvider,
    user: User,
    wearer: Wearer,
    contacts: Sequence[Contact],
) -> None:
    """After the change is committed: text each new contact. A failed SMS never
    undoes the contact; it stays `pending` without `invited_at`, and is logged."""
    for contact in contacts:
        try:
            result = await sms.send(
                SmsMessage(
                    to=contact.phone,
                    body=_invite_text(settings, user, wearer, contact),
                    template_id=settings.sms_invite_template_id,
                )
            )
        except Exception:
            log.exception("invite.sms_failed", contact_id=str(contact.id), to_phone=contact.phone)
            continue
        contact.invited_at = _now()
        log.info(
            "invite.sms_sent",
            contact_id=str(contact.id),
            to_phone=contact.phone,
            is_guardian=contact.is_guardian,
            provider_message_id=result.provider_message_id,
        )
    await session.commit()


# ---------------------------------------------------------------------------- reads


async def list_contacts(session: AsyncSession, user: User, member_id: uuid.UUID) -> list[Contact]:
    wearer = await owned_member(session, user, member_id)
    return await _contacts(session, wearer.id)


# ---------------------------------------------------------------------------- changes


def add_account_holder_contact(session: AsyncSession, user: User, wearer: Wearer) -> Contact:
    """Cohort 1, first in line, guardian; nothing to accept. Caller commits."""
    now = _now()
    contact = Contact(
        id=uuid.uuid4(),
        wearer_id=wearer.id,
        name=user.name or "Account holder",
        phone=user.phone,
        relationship=_ACCOUNT_HOLDER_RELATIONSHIP.get(wearer.relationship, "family"),
        city=user.city or "",
        cohort=1,
        priority=1,
        proximity="strong",
        availability="available",
        is_guardian=True,
        is_account_holder=True,
        contact_user_id=user.id,
        invitation_status="accepted",
        accepted_at=now,
    )
    session.add(contact)
    return contact


def _check_own_number(user: User, wearer: Wearer, phone: str) -> None:
    if phone == user.phone and wearer.is_self:
        raise AppError("own_number", 400)


async def add_contact(
    session: AsyncSession,
    settings: Settings,
    sms: SmsProvider,
    user: User,
    client: ClientInfo,
    member_id: uuid.UUID,
    data: ContactInput,
) -> Contact:
    wearer = await owned_member(session, user, member_id, lock=True)
    contacts = await _contacts(session, wearer.id)
    _check_own_number(user, wearer, data.phone)
    if any(c.phone == data.phone for c in contacts):
        raise AppError("duplicate_phone", 409)
    cohort = _in_cohort(contacts, data.cohort)
    if len(cohort) >= MAX_PER_COHORT:
        raise AppError("cohort_full", 409)

    # The account holder adding themselves back to a family member's list.
    is_account_holder = data.phone == user.phone
    contact = Contact(
        id=uuid.uuid4(),
        wearer_id=wearer.id,
        name=data.name,
        phone=data.phone,
        relationship=data.relationship,
        city=data.city,
        cohort=data.cohort,
        priority=len(cohort) + 1,
        proximity=data.proximity,
        availability=data.availability,
        is_guardian=False,
        is_account_holder=is_account_holder,
        contact_user_id=user.id if is_account_holder else None,
        invitation_status="accepted" if is_account_holder else "pending",
        accepted_at=_now() if is_account_holder else None,
    )
    session.add(contact)
    await _flush(session)
    _audit(
        session,
        user,
        client,
        "contact.created",
        wearer,
        contact.id,
        {
            "phone": contact.phone,  # masked by record_audit
            "cohort": contact.cohort,
            "priority": contact.priority,
            "is_account_holder": is_account_holder,
        },
    )
    await session.commit()
    log.info("contact.created", contact_id=str(contact.id), cohort=contact.cohort)
    if not is_account_holder:
        await send_invites(session, settings, sms, user, wearer, [contact])
    return contact


async def update_contact(
    session: AsyncSession,
    settings: Settings,
    sms: SmsProvider,
    user: User,
    client: ClientInfo,
    member_id: uuid.UUID,
    contact_id: uuid.UUID,
    changes: dict[str, Any],
) -> Contact:
    """`changes` holds only the fields the client sent (EDITABLE + is_guardian)."""
    wearer = await owned_member(session, user, member_id, lock=True)
    contacts = await _contacts(session, wearer.id)
    contact = next((c for c in contacts if c.id == contact_id), None)
    if contact is None:
        raise AppError("not_found", 404)

    changed = sorted(k for k, v in changes.items() if getattr(contact, k) != v)
    new_phone = changes.get("phone", contact.phone)
    if "phone" in changed:
        if contact.is_account_holder:
            raise AppError("account_holder_contact", 409)
        if new_phone == user.phone:
            raise AppError("own_number", 400)
        if any(c.phone == new_phone and c.id != contact.id for c in contacts):
            raise AppError("duplicate_phone", 409)

    old_cohort = contact.cohort
    new_cohort = changes.get("cohort", old_cohort)
    if new_cohort != old_cohort and len(_in_cohort(contacts, new_cohort)) >= MAX_PER_COHORT:
        raise AppError("cohort_full", 409)

    was_guardian = contact.is_guardian
    becomes_guardian = changes.get("is_guardian", was_guardian)
    if becomes_guardian and not was_guardian and _guardians(contacts) >= MAX_GUARDIANS:
        raise AppError("guardian_limit", 409)
    if was_guardian and not becomes_guardian and _guardians(contacts) <= 1:
        raise AppError("last_guardian", 409)

    for key in changed:
        setattr(contact, key, changes[key])
    if new_cohort != old_cohort:
        contact.priority = len(_in_cohort(contacts, new_cohort))  # already counts itself
        _compact(contacts, old_cohort)
    reinvite = "phone" in changed
    if reinvite:
        # A new number is a new person: their invitation starts over.
        contact.invitation_status = "pending"
        contact.contact_user_id = None
        contact.invited_at = contact.accepted_at = contact.declined_at = None

    if changed:
        await _flush(session)
        details: dict[str, Any] = {"fields": changed}
        if "cohort" in changed:
            details["cohort"] = {"from": old_cohort, "to": new_cohort}
        if "is_guardian" in changed:
            details["is_guardian"] = {"from": was_guardian, "to": becomes_guardian}
        if reinvite:
            details["phone"] = new_phone
        _audit(session, user, client, "contact.updated", wearer, contact.id, details)
        await session.commit()
        log.info("contact.updated", contact_id=str(contact.id), fields=changed)
    if reinvite:
        await send_invites(session, settings, sms, user, wearer, [contact])
    return contact


async def remove_contact(
    session: AsyncSession,
    user: User,
    client: ClientInfo,
    member_id: uuid.UUID,
    contact_id: uuid.UUID,
) -> None:
    wearer = await owned_member(session, user, member_id, lock=True)
    contacts = await _contacts(session, wearer.id)
    contact = next((c for c in contacts if c.id == contact_id), None)
    if contact is None:
        raise AppError("not_found", 404)
    if contact.is_guardian and _guardians(contacts) <= 1:
        raise AppError("last_guardian", 409)

    await session.delete(contact)
    remaining = [c for c in contacts if c.id != contact.id]
    _compact(remaining, contact.cohort)
    await _flush(session)
    _audit(
        session,
        user,
        client,
        "contact.removed",
        wearer,
        contact.id,
        {
            "phone": contact.phone,
            "cohort": contact.cohort,
            "was_guardian": contact.is_guardian,
            "was_account_holder": contact.is_account_holder,
            "invitation_status": contact.invitation_status,
        },
    )
    await session.commit()
    log.info("contact.removed", contact_id=str(contact.id), was_guardian=contact.is_guardian)


async def reorder(
    session: AsyncSession,
    user: User,
    client: ClientInfo,
    member_id: uuid.UUID,
    cohort: int,
    ids: Sequence[uuid.UUID],
) -> None:
    """`ids` must be exactly the cohort's contacts, in the new call order."""
    wearer = await owned_member(session, user, member_id, lock=True)
    contacts = await _contacts(session, wearer.id)
    current = _in_cohort(contacts, cohort)
    if len(ids) != len(current) or set(ids) != {c.id for c in current}:
        raise AppError("invalid_order", 400)
    by_id = {c.id: c for c in current}
    before = [str(c.id) for c in current]
    for i, contact_id in enumerate(ids, start=1):
        by_id[contact_id].priority = i
    after = [str(i) for i in ids]
    if before == after:
        return
    await _flush(session)
    _audit(
        session,
        user,
        client,
        "contacts.reordered",
        wearer,
        None,
        {"cohort": cohort, "from": before, "to": after},
    )
    await session.commit()
    log.info("contacts.reordered", cohort=cohort, count=len(ids))


async def copy_contacts(
    session: AsyncSession,
    settings: Settings,
    sms: SmsProvider,
    user: User,
    client: ClientInfo,
    *,
    from_member_id: uuid.UUID,
    to_member_id: uuid.UUID,
) -> list[Contact]:
    """Adds the other member's contacts in their order. Skipped: the account
    holder's own entry, numbers already on the list, and anything over a cohort
    or guardian limit (guardian status is copied while there is room)."""
    if from_member_id == to_member_id:
        raise AppError("same_member", 400)
    source = await owned_member(session, user, from_member_id)
    wearer = await owned_member(session, user, to_member_id, lock=True)
    contacts = await _contacts(session, wearer.id)
    phones = {c.phone for c in contacts}
    guardians = _guardians(contacts)
    counts = {1: len(_in_cohort(contacts, 1)), 2: len(_in_cohort(contacts, 2))}

    added: list[Contact] = []
    skipped: dict[str, int] = {"account_holder": 0, "duplicate": 0, "cohort_full": 0}
    for src in await _contacts(session, source.id):
        if src.is_account_holder or src.phone == user.phone:
            skipped["account_holder"] += 1
            continue
        if src.phone in phones:
            skipped["duplicate"] += 1
            continue
        if counts[src.cohort] >= MAX_PER_COHORT:
            skipped["cohort_full"] += 1
            continue
        guardian = src.is_guardian and guardians < MAX_GUARDIANS
        guardians += guardian
        counts[src.cohort] += 1
        phones.add(src.phone)
        contact = Contact(
            id=uuid.uuid4(),
            wearer_id=wearer.id,
            name=src.name,
            phone=src.phone,
            relationship=src.relationship,
            city=src.city,
            cohort=src.cohort,
            priority=counts[src.cohort],
            proximity=src.proximity,
            availability=src.availability,
            is_guardian=guardian,
            invitation_status="pending",
        )
        session.add(contact)
        added.append(contact)

    if added:
        await _flush(session)
        _audit(
            session,
            user,
            client,
            "contacts.copied",
            wearer,
            None,
            {
                "from_member_id": str(source.id),
                "added": [str(c.id) for c in added],
                "guardians_added": sum(c.is_guardian for c in added),
                "skipped": skipped,
            },
        )
        await session.commit()
        await send_invites(session, settings, sms, user, wearer, added)
    log.info("contacts.copied", from_member_id=str(source.id), added=len(added), skipped=skipped)
    return await _contacts(session, wearer.id)
