from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, Uuid, func, true
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class PushToken(Base):
    """An FCM registration token. A token belongs to whoever registered it last
    (a phone that changes account moves its token)."""

    __tablename__ = "push_tokens"
    __table_args__ = (CheckConstraint("platform IN ('android', 'ios')", name="platform_valid"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token: Mapped[str] = mapped_column(String(4096), unique=True, nullable=False)
    platform: Mapped[str] = mapped_column(String(8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class NotificationPreferences(TimestampMixin, Base):
    """Per user. Emergency alerts are always on and are not stored."""

    __tablename__ = "notification_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    band_offline: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    low_battery: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    test_reminders: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    firmware_updates: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
