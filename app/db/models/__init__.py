"""Import every model here so Alembic autogenerate sees them."""

from app.db.models.audit import AuditLog
from app.db.models.user import User

__all__ = ["AuditLog", "User"]
