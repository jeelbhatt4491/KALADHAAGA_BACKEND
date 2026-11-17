"""Masking of sensitive values before anything is written to a log.

CLAUDE.md: never log OTPs, tokens, device secrets, full phone numbers or exact
locations. This module is applied by a logging filter to *every* record, so a
developer mistake (logging a phone or a token by accident) is still caught.

- Keys that look secret (password, otp, token, secret, signature, ...) -> "***"
- Phone numbers (E.164 and Indian mobiles) -> last 4 digits kept: "+91******3210"
- Coordinates under lat/lon/lng keys -> rounded to 2 decimals (~1 km)
- Live map links /a/<token> -> /a/***
- Bearer tokens / JWTs inside text -> "***"
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

MASK = "***"

_SECRET_KEY_PARTS = (
    "password",
    "passwd",
    "otp",
    "code_hash",
    "token",
    "secret",
    "signature",
    "authorization",
    "api_key",
    "apikey",
    "cookie",
    "nonce",
)
# Exact keys that are secret even though they don't contain one of the parts above.
_SECRET_KEYS = {"code", "pin"}
# Keys whose value is a coordinate.
_COORD_KEYS = {"lat", "lon", "lng", "latitude", "longitude", "home_lat", "home_lng"}
# Keys that are not secret even though they match a part above.
_SAFE_KEYS = {"token_type", "code_length", "otp_purpose", "status_code", "error_code"}

# +<country><number> (8-15 digits) or Indian mobile with optional 91/0 prefix.
_PHONE_RE = re.compile(r"(?<![\w+])(?:\+\d{1,3}[\s-]?\d{6,12}|(?:91|0)?[6-9]\d{9})(?!\w)")
_MAP_LINK_RE = re.compile(r"(/a/)[A-Za-z0-9_\-]{6,}")
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-~+/]+=*")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b")


_SHORT_CODE_RE = re.compile(r"(?<!\w)\d{4,8}(?!\w)")
_PAIR_NONCE_RE = re.compile(r"(KD1\s+PAIR\s+)\S+")


def mask_sms_body(body: str) -> str:
    """SMS bodies may carry OTPs and pairing nonces: hide every 4-8 digit run
    and the `KD1 PAIR <nonce>` value on top of the usual text masking."""
    body = _PAIR_NONCE_RE.sub(lambda m: m.group(1) + MASK, body)
    return _SHORT_CODE_RE.sub(MASK, mask_text(body))


def mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if len(digits) <= 4:
        return MASK
    prefix = "+91" if phone.startswith("+91") else ("+" if phone.startswith("+") else "")
    shown_prefix_digits = len(re.sub(r"\D", "", prefix))
    hidden = len(digits) - 4 - shown_prefix_digits
    return f"{prefix}{'*' * max(hidden, 0)}{digits[-4:]}"


def mask_text(text: str) -> str:
    text = _JWT_RE.sub(MASK, text)
    text = _BEARER_RE.sub(lambda m: m.group(1) + MASK, text)
    text = _MAP_LINK_RE.sub(lambda m: m.group(1) + MASK, text)
    return _PHONE_RE.sub(lambda m: mask_phone(m.group(0)), text)


def _is_secret_key(key: str) -> bool:
    k = key.lower()
    if k in _SAFE_KEYS:
        return False
    return k in _SECRET_KEYS or any(part in k for part in _SECRET_KEY_PARTS)


def mask_value(key: str | None, value: Any) -> Any:
    if key is not None:
        k = key.lower()
        if _is_secret_key(k):
            return MASK if value not in (None, "") else value
        if k in _COORD_KEYS and isinstance(value, int | float) and not isinstance(value, bool):
            return round(float(value), 2)
        if "phone" in k or "msisdn" in k:
            return mask_phone(str(value)) if value else value
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, Mapping):
        return {k2: mask_value(str(k2), v2) for k2, v2 in value.items()}
    if isinstance(value, list | tuple | set):
        return [mask_value(None, v) for v in value]
    return value


def mask_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    return {k: mask_value(k, v) for k, v in data.items()}
