"""The first migration: users table and the append-only audit trail."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import log_context
from app.db.models import AuditLog, User
from app.services.audit import record_audit


async def test_user_round_trip(session: AsyncSession) -> None:
    user = User(phone="+919800000001", name="Asha", password_hash="x")
    session.add(user)
    await session.commit()

    loaded = await session.scalar(select(User).where(User.phone == "+919800000001"))
    assert loaded is not None
    assert loaded.id == user.id
    assert loaded.created_at.tzinfo is not None  # timestamptz, UTC


async def test_phone_must_be_e164(session: AsyncSession) -> None:
    session.add(User(phone="09800000002", password_hash="x"))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_phone_is_unique(session: AsyncSession) -> None:
    session.add(User(phone="+919800000003", password_hash="x"))
    await session.commit()
    session.add(User(phone="+919800000003", password_hash="y"))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_audit_rows_are_masked_and_carry_request_id(session: AsyncSession) -> None:
    with log_context(request_id="req-audit-0001"):
        entry = record_audit(
            session,
            action="auth.signup",
            actor_type="user",
            actor_id="u-1",
            entity_type="user",
            entity_id="u-1",
            ip="203.0.113.7",
            details={"phone": "+919876543210", "otp": "1234"},
        )
    await session.commit()

    stored = await session.scalar(select(AuditLog).where(AuditLog.id == entry.id))
    assert stored is not None
    assert stored.request_id == "req-audit-0001"
    assert stored.details == {"phone": "+91******3210", "otp": "***"}


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_log SET action = 'tampered'",
        "DELETE FROM audit_log",
        "TRUNCATE audit_log",
    ],
)
async def test_audit_log_is_append_only(session: AsyncSession, statement: str) -> None:
    record_audit(session, action="test.row", actor_type="system")
    await session.commit()

    with pytest.raises(DBAPIError, match="append-only"):
        await session.execute(text(statement))
    await session.rollback()
