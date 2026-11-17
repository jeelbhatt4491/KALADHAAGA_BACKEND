"""/dev: development helpers, mounted only when APP_ENV is local or test
(never staging/production; see Settings.dev_endpoints_enabled).

- POST /dev/bands: create sold bands (KD-0001 ...) and return their secrets,
  for scripts/band_sim.py.
- GET /dev/bands/{id}/sms: the SMS the fake provider "sent" to that band's SIM,
  i.e. the band's inbox (`KD1 PAIR <nonce>` ...).
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Path
from pydantic import BaseModel, Field

from app.api.deps import ProvidersDep, SessionDep, SettingsDep
from app.core.errors import AppError
from app.core.logging import get_logger
from app.providers.sms import FakeSmsProvider
from app.services import devices as devices_service

log = get_logger("kaladhaga.dev")
router = APIRouter(prefix="/dev", tags=["dev (local only)"])


class SeedIn(BaseModel):
    start: int = Field(default=1, ge=0, le=9999)
    count: int = Field(default=10, ge=1, le=100)
    prefix: str = Field(default="KD-", pattern=r"^[A-Z0-9-]{1,8}$")
    status: Literal["sold", "unsold"] = "sold"
    reset: bool = Field(default=False, description="Give existing bands a new secret, unlink")


class SeededOut(BaseModel):
    band_id: str
    sim_msisdn: str | None
    status: str
    secret: str | None = Field(description="Only for bands created or reset by this call")


class SmsOut(BaseModel):
    body: str


@router.post("/bands", response_model=list[SeededOut], summary="Create bands to claim")
async def seed(body: SeedIn, session: SessionDep, settings: SettingsDep) -> list[SeededOut]:
    ids = [f"{body.prefix}{n:04d}" for n in range(body.start, body.start + body.count)]
    seeded = await devices_service.seed_bands(
        session, settings, ids, reset=body.reset, status=body.status
    )
    return [
        SeededOut(band_id=b.band_id, sim_msisdn=b.sim_msisdn, status=b.status, secret=b.secret)
        for b in seeded
    ]


@router.get("/bands/{band_id}/sms", response_model=list[SmsOut], summary="The band's SMS inbox")
async def band_inbox(
    band_id: Annotated[str, Path(max_length=32)], session: SessionDep, providers: ProvidersDep
) -> list[SmsOut]:
    device = await devices_service.device_by_band_id(session, band_id)
    if device is None or not isinstance(providers.sms, FakeSmsProvider):
        raise AppError("not_found", 404)
    return [SmsOut(body=m.body) for m in providers.sms.sent if m.to == device.sim_msisdn]
