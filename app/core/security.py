"""Passwords, access tokens and secret hashing.

- Passwords: Argon2id (argon2-cffi defaults), re-hashed on login when parameters change.
- Access tokens: short-lived JWT (HS256) with `sub` = user id.
- OTPs, refresh tokens and reset tokens are stored only as HMAC-SHA256 keyed with
  TOKEN_HASH_SECRET: a leaked database alone cannot be used to log in, and a
  4-digit OTP cannot be brute-forced offline from its hash.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import Settings

_hasher = PasswordHasher()
# Verified against when the phone is unknown, so both paths cost the same.
_DUMMY_HASH = _hasher.hash("not-a-real-password-0")
JWT_ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def hmac_hash(settings: Settings, value: str) -> str:
    key = settings.token_hash_secret.get_secret_value().encode()
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def hashes_match(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


def new_opaque_token() -> str:
    """URL-safe random token (refresh / reset tokens)."""
    return secrets.token_urlsafe(48)


def new_numeric_code(length: int) -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(length))


def create_access_token(settings: Settings, user_id: uuid.UUID) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": str(user_id),
        "typ": "access",
        "iat": now,
        "exp": now + timedelta(seconds=settings.access_token_ttl_s),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm=JWT_ALGORITHM)


class InvalidAccessTokenError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def decode_access_token(settings: Settings, token: str) -> uuid.UUID:
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "exp", "iat", "typ"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise InvalidAccessTokenError("expired") from exc
    except jwt.PyJWTError as exc:
        raise InvalidAccessTokenError("invalid") from exc
    if claims.get("typ") != "access":
        raise InvalidAccessTokenError("wrong_type")
    try:
        return uuid.UUID(str(claims["sub"]))
    except ValueError as exc:
        raise InvalidAccessTokenError("invalid_sub") from exc
