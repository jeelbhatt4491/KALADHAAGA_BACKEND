"""Family members (wearers): add with consent and optional photo, rename, list
with their band and "ready" state.

A band is **ready** only when its member has at least one Cohort 1 contact and
at least one guardian (declined invitations don't count). "Ready" is computed;
it is not the device status (`claimed`).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import Integer, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client import ClientInfo
from app.core.context import bind_log_context
from app.core.errors import AppError
from app.core.logging import get_logger
from app.db.models import Contact, Device, User, Wearer
from app.providers.storage import StorageProvider
from app.services.audit import record_audit

log = get_logger("kaladhaga.family")

# The consent statement shown when adding a family member. Bump the version
# whenever the wording changes; each wearer row stores the version agreed to.
CONSENT_VERSION = "2026-11-17"
CONSENT_TEXT = (
    "I am this person's parent/guardian, or this person, and I agree to their location "
    "and details being shared with their emergency contacts during an alert."
)
CONSENT_SHA256 = hashlib.sha256(CONSENT_TEXT.encode()).hexdigest()

Missing = Literal["band", "cohort1_contact", "guardian"]

_PHOTO_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/heic": "heic"}


@dataclass(frozen=True, slots=True)
class Readiness:
    has_band: bool
    has_cohort1_contact: bool
    has_guardian: bool

    @property
    def ready(self) -> bool:
        return self.has_band and self.has_cohort1_contact and self.has_guardian

    @property
    def missing(self) -> list[Missing]:
        out: list[Missing] = []
        if not self.has_band:
            out.append("band")
        if not self.has_cohort1_contact:
            out.append("cohort1_contact")
        if not self.has_guardian:
            out.append("guardian")
        return out


@dataclass(frozen=True, slots=True)
class MemberView:
    wearer: Wearer
    device: Device | None
    readiness: Readiness


@dataclass(frozen=True, slots=True)
class Photo:
    data: bytes
    content_type: str


def sniff_photo(data: bytes) -> str | None:
    """Content type from the file's own bytes (the client's claim is not trusted)."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypmsf1"):
        return "image/heic"
    return None


# ---------------------------------------------------------------------------- lookups


async def owned_member(
    session: AsyncSession, user: User, member_id: uuid.UUID, *, lock: bool = False
) -> Wearer:
    """The member if it belongs to this account, else 404 (never reveals that
    someone else's member exists). `lock` serialises changes to its contacts."""
    stmt = select(Wearer).where(Wearer.id == member_id, Wearer.account_id == user.id)
    if lock:
        stmt = stmt.with_for_update()
    wearer = await session.scalar(stmt)
    if wearer is None:
        log.info("family.member_not_found", member_id=str(member_id))
        raise AppError("not_found", 404)
    bind_log_context(member_id=str(wearer.id))
    return wearer


async def readiness_for(
    session: AsyncSession, wearer_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[bool, bool]]:
    """wearer id -> (has a Cohort 1 contact, has a guardian); declined don't count."""
    if not wearer_ids:
        return {}
    counted = Contact.invitation_status != "declined"
    rows = await session.execute(
        select(
            Contact.wearer_id,
            func.sum(((Contact.cohort == 1) & counted).cast(Integer)),
            func.sum((Contact.is_guardian & counted).cast(Integer)),
        )
        .where(Contact.wearer_id.in_(wearer_ids))
        .group_by(Contact.wearer_id)
    )
    return {wid: (bool(c1), bool(g)) for wid, c1, g in rows}


async def claimed_devices(
    session: AsyncSession, wearer_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, Device]:
    if not wearer_ids:
        return {}
    devices = await session.scalars(
        select(Device).where(Device.wearer_id.in_(wearer_ids), Device.status == "claimed")
    )
    return {d.wearer_id: d for d in devices if d.wearer_id is not None}


async def views_for(session: AsyncSession, wearers: Sequence[Wearer]) -> list[MemberView]:
    ids = [w.id for w in wearers]
    devices = await claimed_devices(session, ids)
    ready = await readiness_for(session, ids)
    return [
        MemberView(
            wearer=w,
            device=devices.get(w.id),
            readiness=Readiness(w.id in devices, *ready.get(w.id, (False, False))),
        )
        for w in wearers
    ]


async def member_view(session: AsyncSession, wearer: Wearer) -> MemberView:
    return (await views_for(session, [wearer]))[0]


async def list_members(session: AsyncSession, user: User) -> list[MemberView]:
    wearers = (
        await session.scalars(
            select(Wearer)
            .where(Wearer.account_id == user.id)
            # "self" first, then in the order they were added.
            .order_by(Wearer.is_self.desc(), Wearer.created_at, Wearer.id)
        )
    ).all()
    return await views_for(session, wearers)


# ---------------------------------------------------------------------------- changes


async def create_member(
    session: AsyncSession,
    storage: StorageProvider,
    user: User,
    client: ClientInfo,
    *,
    name: str,
    relationship: str,
    age: int,
    gender: str,
    consent: bool,
    photo: Photo | None,
) -> MemberView:
    # Imported here: contacts imports this module for owned_member/readiness.
    from app.services.contacts import add_account_holder_contact

    if not consent:
        log.warning("family.member_refused", reason="consent_missing")
        raise AppError("consent_required", 400)
    is_self = relationship == "self"
    if is_self and await session.scalar(
        select(Wearer.id).where(Wearer.account_id == user.id, Wearer.is_self)
    ):
        raise AppError("self_exists", 409)

    now = datetime.now(UTC)
    wearer = Wearer(
        id=uuid.uuid4(),
        account_id=user.id,
        name=name,
        relationship=relationship,
        age=age,
        gender=gender,
        consent_at=now,
        consent_version=CONSENT_VERSION,
        is_self=is_self,
    )
    session.add(wearer)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        if "uq_wearers_one_self_per_account" in str(exc.orig):
            raise AppError("self_exists", 409) from exc
        raise
    bind_log_context(member_id=str(wearer.id))

    if photo is not None:
        key = f"members/{wearer.id}/{uuid.uuid4().hex}.{_PHOTO_TYPES[photo.content_type]}"
        await storage.put(key, photo.data, photo.content_type)
        wearer.photo_key = key

    # A family member's Cohort 1 starts with the account holder as a guardian;
    # the account holder's own band ("self") starts blank.
    auto_contact = None if is_self else add_account_holder_contact(session, user, wearer)

    record_audit(
        session,
        action="member.created",
        actor_type="user",
        actor_id=str(user.id),
        entity_type="member",
        entity_id=str(wearer.id),
        ip=client.ip,
        details={
            "relationship": relationship,
            "is_self": is_self,
            "consent_at": now.isoformat(),
            "consent_version": CONSENT_VERSION,
            "consent_text_sha256": CONSENT_SHA256,
            "photo": photo is not None,
            "account_holder_contact_id": str(auto_contact.id) if auto_contact else None,
        },
    )
    await session.commit()
    log.info("member.created", relationship=relationship, is_self=is_self, photo=bool(photo))
    return await member_view(session, wearer)


async def rename_member(
    session: AsyncSession, user: User, client: ClientInfo, member_id: uuid.UUID, name: str
) -> MemberView:
    wearer = await owned_member(session, user, member_id, lock=True)
    if wearer.name != name:
        wearer.name = name
        record_audit(
            session,
            action="member.renamed",
            actor_type="user",
            actor_id=str(user.id),
            entity_type="member",
            entity_id=str(wearer.id),
            ip=client.ip,
            details={"fields": ["name"]},  # the names themselves are personal data
        )
        await session.commit()
        log.info("member.renamed")
    return await member_view(session, wearer)
