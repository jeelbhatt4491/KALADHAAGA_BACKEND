"""/auth: sign-up, login, OTP, password reset, token refresh, logout.

None of these take a bearer token (the app sends them with `skipAuth`)."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from fastapi import APIRouter, status
from pydantic import AfterValidator, BaseModel, Field, StringConstraints

from app.api.deps import ClientDep, ProvidersDep, RedisDep, SessionDep, SettingsDep
from app.core.errors import error_responses
from app.core.phone import IndianPhone
from app.services import auth as auth_service
from app.services import otp as otp_service

router = APIRouter(prefix="/auth", tags=["auth"])


def _strong_password(value: str) -> str:
    # Same rule as the app's PasswordRules: at least 8 characters, including a number.
    if len(value) < 8 or not re.search(r"\d", value):
        raise ValueError("password must be at least 8 characters and include a number")
    return value


NewPassword = Annotated[str, Field(max_length=128), AfterValidator(_strong_password)]
OtpCodeStr = Annotated[str, StringConstraints(pattern=r"^\d{4,8}$")]
OpaqueToken = Annotated[str, StringConstraints(min_length=16, max_length=512)]


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str


class LoginRequest(BaseModel):
    phone: IndianPhone
    password: str = Field(min_length=1, max_length=128)


class OtpSendRequest(BaseModel):
    phone: IndianPhone
    purpose: Literal["signup", "reset_password"]


class OtpSendResponse(BaseModel):
    code_length: int
    expires_in: int = Field(description="Seconds until the code expires")


class SignupVerifyRequest(BaseModel):
    phone: IndianPhone
    password: NewPassword
    code: OtpCodeStr


class ResetVerifyRequest(BaseModel):
    phone: IndianPhone
    code: OtpCodeStr


class ResetVerifyResponse(BaseModel):
    reset_token: str


class ResetRequest(BaseModel):
    reset_token: OpaqueToken
    new_password: NewPassword


class RefreshRequest(BaseModel):
    refresh_token: OpaqueToken


class LogoutRequest(BaseModel):
    refresh_token: OpaqueToken
    push_token: str | None = Field(default=None, max_length=4096)


def _tokens(pair: auth_service.TokenPair) -> TokenResponse:
    return TokenResponse(access_token=pair.access_token, refresh_token=pair.refresh_token)


@router.post(
    "/login",
    response_model=TokenResponse,
    responses=error_responses(401, 404, 429),
    summary="Log in with phone + password",
)
async def login(
    body: LoginRequest,
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
    client: ClientDep,
) -> TokenResponse:
    pair = await auth_service.login(
        session, redis, settings, client, phone=body.phone, password=body.password
    )
    return _tokens(pair)


@router.post(
    "/otp/send",
    response_model=OtpSendResponse,
    responses=error_responses(404, 409, 429, 503),
    summary="Send a 4-digit code by SMS (sign-up or password reset)",
)
async def send_otp(
    body: OtpSendRequest,
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
    providers: ProvidersDep,
) -> OtpSendResponse:
    await auth_service.check_otp_allowed(session, phone=body.phone, purpose=body.purpose)
    challenge = await otp_service.send_otp(
        session, redis, settings, providers.sms, phone=body.phone, purpose=body.purpose
    )
    return OtpSendResponse(code_length=challenge.code_length, expires_in=challenge.expires_in)


@router.post(
    "/signup/verify",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(400, 409, 429),
    summary="Verify the sign-up code and create the account",
)
async def signup_verify(
    body: SignupVerifyRequest, session: SessionDep, settings: SettingsDep, client: ClientDep
) -> TokenResponse:
    pair = await auth_service.signup(
        session, settings, client, phone=body.phone, password=body.password, code=body.code
    )
    return _tokens(pair)


@router.post(
    "/password/reset/verify",
    response_model=ResetVerifyResponse,
    responses=error_responses(400, 404, 429),
    summary="Verify the reset code; returns a single-use reset token",
)
async def reset_verify(
    body: ResetVerifyRequest, session: SessionDep, settings: SettingsDep, client: ClientDep
) -> ResetVerifyResponse:
    token = await auth_service.verify_reset_code(
        session, settings, client, phone=body.phone, code=body.code
    )
    return ResetVerifyResponse(reset_token=token)


@router.post(
    "/password/reset",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(400),
    summary="Set a new password (ends every existing login)",
)
async def reset_password(
    body: ResetRequest,
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
    client: ClientDep,
) -> None:
    await auth_service.reset_password(
        session,
        redis,
        settings,
        client,
        reset_token=body.reset_token,
        new_password=body.new_password,
    )


@router.post(
    "/token/refresh",
    response_model=TokenResponse,
    responses=error_responses(401),
    summary="Swap a refresh token for a new token pair (rotation)",
)
async def token_refresh(
    body: RefreshRequest, session: SessionDep, settings: SettingsDep, client: ClientDep
) -> TokenResponse:
    pair = await auth_service.refresh(session, settings, client, refresh_token=body.refresh_token)
    return _tokens(pair)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke this login's refresh token and unregister the push token",
)
async def logout(
    body: LogoutRequest, session: SessionDep, settings: SettingsDep, client: ClientDep
) -> None:
    await auth_service.logout(
        session, settings, client, refresh_token=body.refresh_token, push_token=body.push_token
    )
