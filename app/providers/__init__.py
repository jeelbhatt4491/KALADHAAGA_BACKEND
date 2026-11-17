"""Provider selection. `TELEPHONY_PROVIDER`, `SMS_PROVIDER`, `PUSH_PROVIDER` and
`STORAGE_PROVIDER` choose `fake` or the real one. Real implementations arrive with
the stages that need them (Exotel and FCM after the alert engine runs on fakes,
S3 with hosting); choosing one before then fails at startup instead of silently
doing nothing."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.providers.push import FakePushProvider, PushProvider
from app.providers.sms import FakeSmsProvider, SmsProvider
from app.providers.storage import FakeStorageProvider, StorageProvider
from app.providers.telephony import FakeTelephonyProvider, TelephonyProvider


class ProviderNotAvailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Providers:
    telephony: TelephonyProvider
    sms: SmsProvider
    push: PushProvider
    storage: StorageProvider


def _not_built(kind: str, name: str) -> ProviderNotAvailableError:
    return ProviderNotAvailableError(
        f"{kind} provider '{name}' is not implemented yet; use '{kind.upper()}_PROVIDER=fake'"
    )


def build_providers(settings: Settings) -> Providers:
    telephony: TelephonyProvider
    if settings.telephony_provider == "fake":
        telephony = FakeTelephonyProvider()
    else:
        raise _not_built("telephony", settings.telephony_provider)

    sms: SmsProvider
    if settings.sms_provider == "fake":
        sms = FakeSmsProvider()
    else:
        raise _not_built("sms", settings.sms_provider)

    push: PushProvider
    if settings.push_provider == "fake":
        push = FakePushProvider()
    else:
        raise _not_built("push", settings.push_provider)

    storage: StorageProvider
    if settings.storage_provider == "fake":
        storage = FakeStorageProvider(
            settings.media_dir,
            settings.media_url_secret.get_secret_value(),
            settings.media_url_ttl_s,
        )
    else:  # pragma: no cover - only "fake" exists so far
        raise _not_built("storage", settings.storage_provider)

    return Providers(telephony=telephony, sms=sms, push=push, storage=storage)
