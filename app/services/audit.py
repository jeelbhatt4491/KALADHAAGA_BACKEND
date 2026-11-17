"""Audit trail.

Call `record_audit` for every action that matters as evidence later: sign-up,
login, password reset, band claimed/deactivated, contact added/removed, guardian
changes, alert started/confirmed/ended, test started/ended, admin actions,
provider webhooks.

The row is added to the caller's session, so it commits (or rolls back)
together with the change it describes: the audit table never claims something
happened that did not. The same event is also written to the audit log file.
"""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_log_context
from app.core.logging import AUDIT_LOGGER, get_logger
from app.core.masking import mask_mapping
from app.db.models.audit import AuditLog

ActorType = Literal["user", "device", "system", "admin", "provider"]

_audit_log = get_logger(AUDIT_LOGGER)


def record_audit(
    session: AsyncSession,
    *,
    action: str,
    actor_type: ActorType,
    actor_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    ip: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    ctx = get_log_context()
    entry = AuditLog(
        action=action,
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type=entity_type,
        entity_id=entity_id,
        request_id=ctx.get("request_id"),
        ip=ip,
        details=mask_mapping(details or {}),
    )
    session.add(entry)
    _audit_log.info(
        action,
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type=entity_type,
        entity_id=entity_id,
        details=entry.details,
    )
    return entry
