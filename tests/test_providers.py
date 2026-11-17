from __future__ import annotations

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.core.config import AppEnv, Settings
from app.providers import ProviderNotAvailableError, build_providers
from app.providers.push import FakePushProvider, PushMessage
from app.providers.sms import FakeSmsProvider, SmsMessage
from app.providers.telephony import CallRequest, FakeTelephonyProvider


async def test_app_uses_fake_providers_in_tests(app: FastAPI) -> None:
    providers = app.state.providers
    assert isinstance(providers.telephony, FakeTelephonyProvider)
    assert isinstance(providers.sms, FakeSmsProvider)
    assert isinstance(providers.push, FakePushProvider)


async def test_fakes_record_what_they_would_have_sent() -> None:
    telephony, sms, push = FakeTelephonyProvider(), FakeSmsProvider(), FakePushProvider()

    handle = await telephony.place_call(
        CallRequest(to="+919876543210", alert_id="al-1", conference_id="conf-1", is_test=True)
    )
    await telephony.hang_up(handle.provider_call_id)
    await sms.send(SmsMessage(to="+919876543210", body="TEST: Asha needs help", is_test=True))
    result = await push.send(PushMessage(device_tokens=("t1", "t2"), title="TEST", body="Asha"))

    assert telephony.calls[0].to == "+919876543210"
    assert telephony.hung_up == [handle.provider_call_id]
    assert sms.sent[0].body.startswith("TEST")
    assert result.success_count == 2


def test_test_env_refuses_real_providers() -> None:
    with pytest.raises(ValidationError, match="fake providers"):
        Settings(app_env=AppEnv.TEST, sms_provider="exotel")


def test_unbuilt_real_provider_fails_loudly() -> None:
    settings = Settings(app_env=AppEnv.LOCAL, telephony_provider="exotel")
    with pytest.raises(ProviderNotAvailableError, match="telephony"):
        build_providers(settings)


def test_deployed_env_requires_jwt_secret() -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(app_env=AppEnv.PRODUCTION)
