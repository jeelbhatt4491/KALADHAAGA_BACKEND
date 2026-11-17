"""Family members (wearers), bands, pairings and emergency contacts.

The API says "member" (`/family/members`, `member_id`); the database says `wearers`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

RELATIONSHIPS = ("self", "son", "daughter", "wife", "husband", "mother", "father", "other")
DEVICE_STATUSES = ("unsold", "sold", "claimed", "deactivated")
SIGNALS = ("good", "fair", "poor")
PROXIMITIES = ("strong", "moderate", "low")
AVAILABILITIES = ("available", "limited", "unavailable")
INVITATION_STATUSES = ("pending", "accepted", "declined")

MAX_PER_COHORT = 5
MAX_GUARDIANS = 2


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Wearer(TimestampMixin, Base):
    """A family member who wears a band. Often has no phone of their own."""

    __tablename__ = "wearers"
    __table_args__ = (
        CheckConstraint(_in("relationship", RELATIONSHIPS), name="relationship_valid"),
        CheckConstraint("gender IN ('female', 'male', 'other')", name="gender_valid"),
        CheckConstraint("age BETWEEN 0 AND 120", name="age_valid"),
        CheckConstraint("is_self = (relationship = 'self')", name="self_matches_relationship"),
        # One "self" profile per account.
        Index(
            "uq_wearers_one_self_per_account",
            "account_id",
            unique=True,
            postgresql_where=text("is_self"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    relationship: Mapped[str] = mapped_column(String(16), nullable=False)
    age: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    gender: Mapped[str] = mapped_column(String(8), nullable=False)
    photo_key: Mapped[str | None] = mapped_column(String(255))
    # When the account holder agreed, and to which wording of the consent statement.
    consent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consent_version: Mapped[str] = mapped_column(String(32), nullable=False)
    is_self: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())


class Device(TimestampMixin, Base):
    """A band. `band_id` (e.g. KD-0001) is printed on it and is the `{id}` in
    `/devices/{id}/...`. Status: unsold -> sold -> claimed -> deactivated
    (a deactivated band can be claimed again)."""

    __tablename__ = "devices"
    __table_args__ = (
        CheckConstraint(_in("status", DEVICE_STATUSES), name="status_valid"),
        CheckConstraint(_in("signal", SIGNALS), name="signal_valid"),
        CheckConstraint("battery_percent BETWEEN 0 AND 100", name="battery_valid"),
        CheckConstraint(r"sim_msisdn ~ '^\+[1-9][0-9]{7,14}$'", name="sim_msisdn_e164"),
        CheckConstraint(
            "(status = 'claimed') = (wearer_id IS NOT NULL)", name="claimed_has_wearer"
        ),
        # One claimed band per wearer.
        Index(
            "uq_devices_one_claimed_per_wearer",
            "wearer_id",
            unique=True,
            postgresql_where=text("status = 'claimed'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    band_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    imei: Mapped[str | None] = mapped_column(String(20), unique=True)
    # The band's own SIM: where `KD1 ...` command SMS go.
    sim_msisdn: Mapped[str | None] = mapped_column(String(16))
    # Signing secret, encrypted with DEVICE_SECRET_KEY (AES-GCM, band_id as associated data).
    # The backend needs the secret itself to check HMAC signatures, so it can't be a hash.
    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="unsold")
    wearer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("wearers.id", ondelete="RESTRICT"), index=True
    )
    firmware_version: Mapped[str | None] = mapped_column(String(32))
    battery_percent: Mapped[int | None] = mapped_column(SmallInteger)
    signal: Mapped[str | None] = mapped_column(String(8))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sold_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Last claim ("linked_at" in the API).
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DevicePairing(Base):
    """One vibrate check: the backend texts `KD1 PAIR <nonce>`, the band pulses
    and answers `/device/v1/pair-ack`, the user confirms they felt it."""

    __tablename__ = "device_pairings"
    __table_args__ = (Index("ix_device_pairings_device_created", "device_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    nonce_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # What the user answered to "Did you feel the pulse?".
    felt: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Contact(TimestampMixin, Base):
    """An emergency contact of one wearer. Cohort 1 / 2, up to 5 each, in call
    order (`priority`); up to 2 guardians. Limits are checked by the service and
    again by a database trigger (migration 0003)."""

    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("wearer_id", "phone", name="uq_contacts_wearer_phone"),
        # Deferred so a reorder can swap positions inside one transaction.
        UniqueConstraint(
            "wearer_id",
            "cohort",
            "priority",
            name="uq_contacts_wearer_cohort_priority",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("cohort IN (1, 2)", name="cohort_valid"),
        CheckConstraint("priority >= 1", name="priority_valid"),
        CheckConstraint(r"phone ~ '^\+[1-9][0-9]{7,14}$'", name="phone_e164"),
        CheckConstraint(_in("proximity", PROXIMITIES), name="proximity_valid"),
        CheckConstraint(_in("availability", AVAILABILITIES), name="availability_valid"),
        CheckConstraint(_in("invitation_status", INVITATION_STATUSES), name="invitation_valid"),
        Index("ix_contacts_phone_invitation", "phone", "invitation_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    wearer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wearers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    phone: Mapped[str] = mapped_column(String(16), nullable=False)
    relationship: Mapped[str] = mapped_column(String(50), nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False, server_default="")
    cohort: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    proximity: Mapped[str] = mapped_column(String(16), nullable=False)
    availability: Mapped[str] = mapped_column(String(16), nullable=False)
    is_guardian: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    is_account_holder: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    # The app user who accepted the invitation (null until then).
    contact_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    invitation_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending"
    )
    invited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    declined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
