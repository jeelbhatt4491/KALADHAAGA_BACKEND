"""Telephony (Exotel in production): outbound calls into a conference with an
IVR "press 1 to listen" step. The interface is a first cut; it will grow in B2
(call rounds) once the Exotel flow is designed."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol

from app.core.logging import get_logger


@dataclass(frozen=True, slots=True)
class CallRequest:
    to: str  # E.164
    alert_id: str
    conference_id: str
    is_test: bool  # the IVR must say TEST


@dataclass(frozen=True, slots=True)
class CallHandle:
    provider_call_id: str


class TelephonyProvider(Protocol):
    name: str

    async def place_call(self, request: CallRequest) -> CallHandle: ...

    async def hang_up(self, provider_call_id: str) -> None: ...

    async def end_conference(self, conference_id: str) -> None: ...


@dataclass
class FakeTelephonyProvider:
    """Logs instead of calling. Keeps everything in memory for tests."""

    name: str = "fake"
    calls: list[CallRequest] = field(default_factory=list)
    hung_up: list[str] = field(default_factory=list)
    ended_conferences: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._log = get_logger("kaladhaga.providers.telephony.fake")

    async def place_call(self, request: CallRequest) -> CallHandle:
        handle = CallHandle(provider_call_id=f"fake-call-{uuid.uuid4().hex[:12]}")
        self.calls.append(request)
        self._log.info(
            "telephony.call_placed",
            provider=self.name,
            to_phone=request.to,
            alert_id=request.alert_id,
            conference_id=request.conference_id,
            is_test=request.is_test,
            provider_call_id=handle.provider_call_id,
        )
        return handle

    async def hang_up(self, provider_call_id: str) -> None:
        self.hung_up.append(provider_call_id)
        self._log.info(
            "telephony.call_hung_up", provider=self.name, provider_call_id=provider_call_id
        )

    async def end_conference(self, conference_id: str) -> None:
        self.ended_conferences.append(conference_id)
        self._log.info(
            "telephony.conference_ended", provider=self.name, conference_id=conference_id
        )
