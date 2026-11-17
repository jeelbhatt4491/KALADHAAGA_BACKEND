"""Import every model here so Alembic autogenerate sees them."""

from app.db.models.audit import AuditLog
from app.db.models.auth import OtpCode, PasswordResetToken, RefreshToken
from app.db.models.family import Contact, Device, DevicePairing, Wearer
from app.db.models.notifications import NotificationPreferences, PushToken
from app.db.models.user import User

__all__ = [
    "AuditLog",
    "Contact",
    "Device",
    "DevicePairing",
    "NotificationPreferences",
    "OtpCode",
    "PasswordResetToken",
    "PushToken",
    "RefreshToken",
    "User",
    "Wearer",
]
