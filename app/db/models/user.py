from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

ROLES = ("account_holder", "contact")
GENDERS = ("female", "male", "other")


class User(TimestampMixin, Base):
    """An app login (account holder and/or contact). Login = phone + password;
    OTP only verifies the number at sign-up and for password reset."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(r"phone ~ '^\+[1-9][0-9]{7,14}$'", name="phone_e164"),
        CheckConstraint("role IN ('account_holder', 'contact')", name="role_valid"),
        CheckConstraint("gender IN ('female', 'male', 'other')", name="gender_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    phone: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Chosen on the role screen; null until then (onboarding_state = needs_role).
    role: Mapped[str | None] = mapped_column(String(16))
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    gender: Mapped[str | None] = mapped_column(String(8))
    city: Mapped[str | None] = mapped_column(String(100))
