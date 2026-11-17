"""SMS (Exotel SMS with DLT templates in production). Also carries the
backend -> band commands (`KD1 PAIR <nonce>`, `KD1 FAM <alert_id>`, ...)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol

from app.core.logging import get_logger
from app.core.masking import mask_sms_body


@dataclass(frozen=True, slots=True)
class SmsMessage:
    to: str  # E.164
    body: str
    # DLT template id (required by Indian regulation for real sends).
    template_id: str | None = None
    is_test: bool = False


@dataclass(frozen=True, slots=True)
class SmsResult:
    provider_message_id: str


class SmsProvider(Protocol):
    name: str

    async def send(self, message: SmsMessage) -> SmsResult: ...


@dataclass
class FakeSmsProvider:
    """Logs instead of sending. Keeps everything in memory for tests."""

    name: str = "fake"
    sent: list[SmsMessage] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._log = get_logger("kaladhaga.providers.sms.fake")

    async def send(self, message: SmsMessage) -> SmsResult:
        result = SmsResult(provider_message_id=f"fake-sms-{uuid.uuid4().hex[:12]}")
        self.sent.append(message)
        self._log.info(
            "sms.sent",
            provider=self.name,
            to_phone=message.to,
            template_id=message.template_id,
            is_test=message.is_test,
            body=mask_sms_body(message.body),
            body_length=len(message.body),
            provider_message_id=result.provider_message_id,
        )
        return result
