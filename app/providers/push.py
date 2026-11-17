"""Push notifications (Firebase Cloud Messaging in production). Alert pushes
use the app's `emergency_alerts` Android channel."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.core.logging import get_logger

EMERGENCY_CHANNEL_ID = "emergency_alerts"


@dataclass(frozen=True, slots=True)
class PushMessage:
    device_tokens: tuple[str, ...]
    title: str
    body: str
    data: dict[str, str] = field(default_factory=dict)
    high_priority: bool = False
    android_channel_id: str | None = None
    is_test: bool = False


@dataclass(frozen=True, slots=True)
class PushResult:
    success_count: int
    failure_count: int
    # Tokens FCM reported as no longer valid (to delete from the database).
    invalid_tokens: tuple[str, ...] = ()


class PushProvider(Protocol):
    name: str

    async def send(self, message: PushMessage) -> PushResult: ...


@dataclass
class FakePushProvider:
    """Logs instead of pushing. Keeps everything in memory for tests."""

    name: str = "fake"
    sent: list[PushMessage] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._log = get_logger("kaladhaga.providers.push.fake")

    async def send(self, message: PushMessage) -> PushResult:
        self.sent.append(message)
        self._log.info(
            "push.sent",
            provider=self.name,
            recipients=len(message.device_tokens),
            title=message.title,
            high_priority=message.high_priority,
            android_channel_id=message.android_channel_id,
            is_test=message.is_test,
            data_keys=sorted(message.data),
        )
        return PushResult(success_count=len(message.device_tokens), failure_count=0)
