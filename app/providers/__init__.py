"""Provider selection. `TELEPHONY_PROVIDER`, `SMS_PROVIDER` and `PUSH_PROVIDER`
choose `fake` or the real one. Real implementations arrive with the stages that
need them (Exotel and FCM after the alert engine runs on fakes); choosing one before then fails at
startup instead of silently doing nothing."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.providers.push import FakePushProvider, PushProvider
from app.providers.sms import FakeSmsProvider, SmsProvider
from app.providers.telephony import FakeTelephonyProvider, TelephonyProvider


class ProviderNotAvailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Providers:
    telephony: TelephonyProvider
    sms: SmsProvider
    push: PushProvider


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

    return Providers(telephony=telephony, sms=sms, push=push)
