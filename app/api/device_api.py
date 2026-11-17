"""/device/v1: endpoints the band calls. Every request is signed (see
app/core/device_auth.py); the signature is checked before the body is parsed.
TODO(firmware): field names are proposals until the firmware side confirms them.
"""

from __future__ import annotations

import hashlib
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError

from app.api.deps import RedisDep, SessionDep, SettingsDep
from app.api.types import Signal
from app.core.context import bind_log_context
from app.core.device_auth import signature_valid
from app.core.errors import AppError, error_responses
from app.core.logging import get_logger
from app.db.models import Device
from app.services import devices as devices_service

log = get_logger("kaladhaga.device_api")
router = APIRouter(prefix="/device/v1", tags=["band"], responses=error_responses(400, 401, 422))


class SignedRequest:
    def __init__(self, device: Device, body: bytes) -> None:
        self.device = device
        self.body = body

    def parse[M: BaseModel](self, model: type[M]) -> M:
        try:
            return model.model_validate_json(self.body or b"{}")
        except ValidationError as exc:
            raise RequestValidationError(exc.errors()) from exc


def _reject(reason: str, **fields: Any) -> AppError:
    log.warning("device.auth_rejected", reason=reason, **fields)
    return AppError("invalid_signature", 401)


async def _signed(
    request: Request, session: SessionDep, redis: RedisDep, settings: SettingsDep
) -> SignedRequest:
    band_id = request.headers.get("x-device-id", "")
    timestamp = request.headers.get("x-timestamp", "")
    signature = request.headers.get("x-signature", "")
    if not (band_id and timestamp and signature):
        raise _reject("missing_headers")
    bind_log_context(device_id=band_id.strip().upper()[:32])
    try:
        skew = abs(time.time() - int(timestamp))
    except ValueError:
        raise _reject("bad_timestamp") from None
    if skew > settings.device_signature_max_skew_s:
        raise _reject("stale_timestamp", skew_s=int(skew))

    device = await devices_service.device_by_band_id(session, band_id)
    if device is None:
        raise _reject("unknown_device")
    body = await request.body()
    if not signature_valid(
        devices_service.device_secret(settings, device), timestamp, body, signature
    ):
        raise _reject("bad_signature")
    # Each signature is accepted once while its timestamp is still valid.
    replay_key = f"device:sig:{hashlib.sha256(signature.encode()).hexdigest()[:32]}"
    if not await redis.set(replay_key, "1", ex=2 * settings.device_signature_max_skew_s, nx=True):
        raise _reject("replayed")
    return SignedRequest(device, body)


Signed = Annotated[SignedRequest, Depends(_signed)]


class HeartbeatIn(BaseModel):
    battery_percent: int = Field(ge=0, le=100)
    signal: Signal
    firmware_version: str | None = Field(default=None, max_length=32)


class HeartbeatOut(BaseModel):
    server_time: int = Field(description="Unix seconds, so the band can correct its clock")


class PairAckIn(BaseModel):
    nonce: str = Field(min_length=4, max_length=32, description="From `KD1 PAIR <nonce>`")


@router.post("/heartbeat", response_model=HeartbeatOut, summary="Band status (battery, signal)")
async def heartbeat(signed: Signed, session: SessionDep) -> HeartbeatOut:
    body = signed.parse(HeartbeatIn)
    await devices_service.heartbeat(
        session,
        signed.device,
        battery_percent=body.battery_percent,
        signal=body.signal,
        firmware_version=body.firmware_version,
    )
    return HeartbeatOut(server_time=int(time.time()))


@router.post(
    "/pair-ack",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="The band received `KD1 PAIR <nonce>` and pulsed",
)
async def pair_ack(signed: Signed, session: SessionDep, settings: SettingsDep) -> None:
    body = signed.parse(PairAckIn)
    await devices_service.pair_ack(session, settings, signed.device, body.nonce)
