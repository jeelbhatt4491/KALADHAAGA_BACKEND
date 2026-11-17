"""Bands: availability, vibrate check, claim, deactivate; the band's own
heartbeat and pair-ack; dev seeding.

Status: unsold -> sold (Shopify webhook; `seed_bands` in development) ->
claimed (linked to one member) -> deactivated (released; can be claimed again).
One claimed band per member (partial unique index).

Vibrate check (QR / typed code path; over Bluetooth the app pulses the band
itself): the backend texts the band `KD1 PAIR <nonce>`, the band pulses and
answers `POST /device/v1/pair-ack {nonce}`, the user confirms they felt it.
Claim records which proof existed (`proof` in the audit row).
TODO(security): a claim needs only the band ID today, because the Bluetooth path
gives the backend no proof of possession. Once the firmware can sign a challenge
over Bluetooth, require a proof for every claim.
"""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client import ClientInfo
from app.core.config import Settings
from app.core.context import bind_log_context
from app.core.device_auth import (
    decrypt_device_secret,
    encrypt_device_secret,
    new_device_secret,
    normalize_band_id,
)
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.rate_limit import acquire_cooldown, hit_counter, seconds_left
from app.core.security import hmac_hash
from app.db.models import Device, DevicePairing, User, Wearer
from app.providers.sms import SmsMessage, SmsProvider
from app.services import family
from app.services.audit import record_audit

log = get_logger("kaladhaga.devices")

CLAIMABLE = ("sold", "deactivated")
Proof = Literal["felt_confirmed", "band_acked", "none"]


def _now() -> datetime:
    return datetime.now(UTC)


def is_connected(settings: Settings, device: Device) -> bool:
    return device.last_seen_at is not None and _now() - device.last_seen_at <= timedelta(
        seconds=settings.device_online_timeout_s
    )


def _version(v: str) -> tuple[int, ...] | None:
    parts = re.findall(r"\d+", v)
    return tuple(int(p) for p in parts) if parts else None


def firmware_up_to_date(settings: Settings, device: Device) -> bool | None:
    latest, current = settings.latest_firmware_version, device.firmware_version
    if not latest or not current:
        return None
    lv, cv = _version(latest), _version(current)
    return cv >= lv if lv is not None and cv is not None else current == latest


def _nonce_hash(settings: Settings, device: Device, nonce: str) -> str:
    return hmac_hash(settings, f"pair:{device.id}:{nonce.upper()}")


async def _device(session: AsyncSession, band_id: str, *, lock: bool = False) -> Device | None:
    stmt = select(Device).where(Device.band_id == normalize_band_id(band_id))
    if lock:
        stmt = stmt.with_for_update()
    device: Device | None = await session.scalar(stmt)
    if device is not None:
        bind_log_context(device_id=device.band_id)
    return device


def _check_claimable(device: Device | None, band_id: str) -> Device:
    """The errors the pairing screens know: not_found, not_sold, already_active."""
    if device is None:
        log.info("device.lookup_failed", band_id=band_id, reason="not_found")
        raise AppError("not_found", 404)
    if device.status == "unsold":
        log.info("device.lookup_failed", reason="not_sold")
        raise AppError("not_sold", 409)
    if device.status == "claimed":
        log.info("device.lookup_failed", reason="already_active")
        linked = device.activated_at.isoformat() if device.activated_at else None
        raise AppError("already_active", 409, linked_at=linked)
    return device


async def _owner_id(session: AsyncSession, device: Device) -> uuid.UUID | None:
    if device.wearer_id is None:
        return None
    owner: uuid.UUID | None = await session.scalar(
        select(Wearer.account_id).where(Wearer.id == device.wearer_id)
    )
    return owner


# ---------------------------------------------------------------------------- app side


async def availability(
    session: AsyncSession, redis: Redis, settings: Settings, user: User, band_id: str
) -> Device:
    key = f"devices:lookup:{user.id}"
    if await hit_counter(redis, key, 3600) > settings.availability_max_per_hour:
        log.warning("device.lookup_throttled")
        raise AppError("too_many_attempts", 429, retry_after=await seconds_left(redis, key))
    device = _check_claimable(await _device(session, band_id), band_id)
    log.info("device.available", status=device.status)
    return device


@dataclass(frozen=True, slots=True)
class VibrateCheck:
    pairing_id: uuid.UUID
    expires_in: int


async def vibrate_check(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    sms: SmsProvider,
    user: User,
    client: ClientInfo,
    band_id: str,
) -> VibrateCheck:
    device = await _device(session, band_id)
    # A free band, or one already on this account ("find my band").
    if device is None or device.status != "claimed" or await _owner_id(session, device) != user.id:
        device = _check_claimable(device, band_id)
    if not device.sim_msisdn:
        log.error("device.vibrate_check_failed", reason="no_sim_number")
        raise AppError("band_unreachable", 409)

    cooldown = await acquire_cooldown(
        redis, f"devices:vibrate:cooldown:{device.id}", settings.vibrate_check_cooldown_s
    )
    if cooldown:
        raise AppError("too_many_attempts", 429, retry_after=cooldown)
    hourly = f"devices:vibrate:hourly:{device.id}"
    if await hit_counter(redis, hourly, 3600) > settings.vibrate_check_max_per_hour:
        log.warning("device.vibrate_check_throttled", reason="hourly_cap")
        raise AppError("too_many_attempts", 429, retry_after=await seconds_left(redis, hourly))

    nonce = secrets.token_hex(4).upper()
    pairing = DevicePairing(
        id=uuid.uuid4(),
        device_id=device.id,
        requested_by_user_id=user.id,
        nonce_hash=_nonce_hash(settings, device, nonce),
        expires_at=_now() + timedelta(seconds=settings.pairing_ttl_s),
    )
    session.add(pairing)
    record_audit(
        session,
        action="device.vibrate_check_requested",
        actor_type="user",
        actor_id=str(user.id),
        entity_type="device",
        entity_id=device.band_id,
        ip=client.ip,
        details={"pairing_id": str(pairing.id), "device_status": device.status},
    )
    await session.commit()

    try:
        result = await sms.send(SmsMessage(to=device.sim_msisdn, body=f"KD1 PAIR {nonce}"))
    except Exception as exc:
        log.exception("device.vibrate_check_sms_failed", pairing_id=str(pairing.id))
        raise AppError("sms_unavailable", 503) from exc
    log.info(
        "device.vibrate_check_sent",
        pairing_id=str(pairing.id),
        provider_message_id=result.provider_message_id,
    )
    return VibrateCheck(pairing_id=pairing.id, expires_in=settings.pairing_ttl_s)


async def _latest_pairing(
    session: AsyncSession, device: Device, user: User, *, within: timedelta | None = None
) -> DevicePairing | None:
    stmt = (
        select(DevicePairing)
        .where(DevicePairing.device_id == device.id, DevicePairing.requested_by_user_id == user.id)
        .order_by(DevicePairing.created_at.desc())
        .limit(1)
    )
    if within is not None:
        stmt = stmt.where(DevicePairing.created_at >= _now() - within)
    pairing: DevicePairing | None = await session.scalar(stmt)
    return pairing


async def confirm_vibration(
    session: AsyncSession, user: User, client: ClientInfo, band_id: str, *, felt: bool
) -> DevicePairing:
    """The user's answer to "Did you feel the pulse?" for their latest vibrate check."""
    device = await _device(session, band_id)
    if device is None:
        raise AppError("not_found", 404)
    pairing = await _latest_pairing(session, device, user)
    if pairing is None or pairing.expires_at <= _now():
        log.info("device.vibration_confirm_refused", reason="no_open_pairing")
        raise AppError("pairing_expired", 409)
    pairing.confirmed_at = _now()
    pairing.felt = felt
    record_audit(
        session,
        action="device.vibration_confirmed",
        actor_type="user",
        actor_id=str(user.id),
        entity_type="device",
        entity_id=device.band_id,
        ip=client.ip,
        details={"pairing_id": str(pairing.id), "felt": felt, "band_acked": bool(pairing.acked_at)},
    )
    await session.commit()
    log.info("device.vibration_confirmed", felt=felt, band_acked=bool(pairing.acked_at))
    return pairing


async def claim(
    session: AsyncSession,
    settings: Settings,
    user: User,
    client: ClientInfo,
    *,
    band_id: str,
    member_id: uuid.UUID,
) -> family.MemberView:
    wearer = await family.owned_member(session, user, member_id, lock=True)
    device = _check_claimable(await _device(session, band_id, lock=True), band_id)
    if await family.claimed_devices(session, [wearer.id]):
        raise AppError("member_has_band", 409)

    pairing = await _latest_pairing(
        session, device, user, within=timedelta(seconds=settings.pairing_ttl_s * 6)
    )
    proof: Proof = "none"
    if pairing is not None and pairing.felt:
        proof = "felt_confirmed"
    elif pairing is not None and pairing.acked_at:
        proof = "band_acked"

    previous_status = device.status
    device.status = "claimed"
    device.wearer_id = wearer.id
    device.activated_at = _now()
    device.deactivated_at = None
    try:
        await session.flush()
    except IntegrityError as exc:  # another band was claimed for this member meanwhile
        await session.rollback()
        if "uq_devices_one_claimed_per_wearer" in str(exc.orig):
            raise AppError("member_has_band", 409) from exc
        raise
    record_audit(
        session,
        action="device.claimed",
        actor_type="user",
        actor_id=str(user.id),
        entity_type="device",
        entity_id=device.band_id,
        ip=client.ip,
        details={
            "member_id": str(wearer.id),
            "previous_status": previous_status,
            "proof": proof,
            "pairing_id": str(pairing.id) if pairing else None,
        },
    )
    await session.commit()
    log.info("device.claimed", previous_status=previous_status, proof=proof)
    return await family.member_view(session, wearer)


async def deactivate(session: AsyncSession, user: User, client: ClientInfo, band_id: str) -> None:
    device = await _device(session, band_id, lock=True)
    # Only the account that owns the band; anyone else sees not_found.
    if device is None or device.status != "claimed" or await _owner_id(session, device) != user.id:
        raise AppError("not_found", 404)
    # TODO(B3): refuse while an alert on this band is open.
    member_id = device.wearer_id
    device.status = "deactivated"
    device.wearer_id = None
    device.deactivated_at = _now()
    record_audit(
        session,
        action="device.deactivated",
        actor_type="user",
        actor_id=str(user.id),
        entity_type="device",
        entity_id=device.band_id,
        ip=client.ip,
        details={"member_id": str(member_id)},
    )
    await session.commit()
    log.info("device.deactivated", member_id=str(member_id))


# ---------------------------------------------------------------------------- band side


async def device_by_band_id(session: AsyncSession, band_id: str) -> Device | None:
    return await _device(session, band_id)


def device_secret(settings: Settings, device: Device) -> str:
    return decrypt_device_secret(settings, device.band_id, device.secret_ciphertext)


async def heartbeat(
    session: AsyncSession,
    device: Device,
    *,
    battery_percent: int,
    signal: str,
    firmware_version: str | None,
) -> None:
    was_seen = device.last_seen_at
    device.last_seen_at = _now()
    device.battery_percent = battery_percent
    device.signal = signal
    if firmware_version and firmware_version != device.firmware_version:
        log.info("device.firmware_changed", old=device.firmware_version, new=firmware_version)
        device.firmware_version = firmware_version
    await session.commit()
    # INFO on purpose: proof the band was alive and reachable at this moment.
    log.info(
        "device.heartbeat",
        battery_percent=battery_percent,
        signal=signal,
        firmware_version=device.firmware_version,
        first_contact=was_seen is None,
    )


async def pair_ack(session: AsyncSession, settings: Settings, device: Device, nonce: str) -> None:
    pairing: DevicePairing | None = await session.scalar(
        select(DevicePairing)
        .where(
            DevicePairing.device_id == device.id,
            DevicePairing.nonce_hash == _nonce_hash(settings, device, nonce),
        )
        .with_for_update()
    )
    if pairing is None or pairing.expires_at <= _now():
        log.warning("device.pair_ack_rejected", reason="unknown" if pairing is None else "expired")
        raise AppError("invalid_nonce", 400)
    if pairing.acked_at is None:
        pairing.acked_at = _now()
        record_audit(
            session,
            action="device.pair_acked",
            actor_type="device",
            actor_id=device.band_id,
            entity_type="device",
            entity_id=device.band_id,
            details={"pairing_id": str(pairing.id)},
        )
        await session.commit()
    log.info("device.pair_acked", pairing_id=str(pairing.id))


# ---------------------------------------------------------------------------- dev seeding


def dev_sim_number(band_id: str) -> str:
    """A made-up SIM number for a seeded band: KD-0001 -> +917000000001."""
    digits = re.sub(r"\D", "", band_id)[-9:]
    return f"+917{digits.zfill(9)}"


@dataclass(frozen=True, slots=True)
class SeededBand:
    band_id: str
    sim_msisdn: str | None
    status: str
    # Only for bands created (or reset) by this call; otherwise unknown here.
    secret: str | None


async def seed_bands(
    session: AsyncSession,
    settings: Settings,
    band_ids: list[str],
    *,
    reset: bool = False,
    status: Literal["sold", "unsold"] = "sold",
) -> list[SeededBand]:
    """Development only: create bands ready to claim. Existing bands are left as
    they are unless `reset` (new secret, back to `status`, unlinked)."""
    if settings.is_deployed:
        raise RuntimeError("seed_bands is for local development and tests only")
    out: list[SeededBand] = []
    now = _now()
    for raw in band_ids:
        band_id = normalize_band_id(raw)
        device = await _device(session, band_id, lock=True)
        existed = device is not None
        if device is not None and not reset:
            out.append(SeededBand(band_id, device.sim_msisdn, device.status, None))
            continue
        secret = new_device_secret()
        values: dict[str, Any] = {
            "secret_ciphertext": encrypt_device_secret(settings, band_id, secret),
            "status": status,
            "wearer_id": None,
            "sold_at": now if status == "sold" else None,
            "activated_at": None,
            "deactivated_at": None,
            "sim_msisdn": dev_sim_number(band_id),
        }
        if device is None:
            device = Device(id=uuid.uuid4(), band_id=band_id, **values)
            session.add(device)
        else:
            for key, value in values.items():
                setattr(device, key, value)
        await session.flush()
        record_audit(
            session,
            action="device.seeded",
            actor_type="system",
            entity_type="device",
            entity_id=band_id,
            details={"status": status, "reset": existed},
        )
        out.append(SeededBand(band_id, device.sim_msisdn, status, secret))
    await session.commit()
    log.info("device.seeded", count=len(band_ids), created=sum(b.secret is not None for b in out))
    return out
