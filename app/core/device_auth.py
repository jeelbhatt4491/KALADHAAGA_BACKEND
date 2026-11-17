"""Band secrets and request signatures.

Each band has its own random signing secret, flashed at the factory. The
backend must know the secret to check signatures (HMAC is symmetric), so it is
stored encrypted (AES-256-GCM with DEVICE_SECRET_KEY, the band ID as associated
data so a ciphertext cannot be moved to another band's row).

Band requests carry:
    X-Device-Id:  band ID (KD-0001)
    X-Timestamp:  unix seconds
    X-Signature:  hex HMAC-SHA256(secret, f"{timestamp}{raw body}")
Requests more than DEVICE_SIGNATURE_MAX_SKEW_S off are rejected; a signature is
accepted once (replays within the window are rejected).
TODO(firmware): confirm header names and the signed string with the firmware side.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import Settings

_NONCE_BYTES = 12


def _aead(settings: Settings) -> AESGCM:
    key = hashlib.sha256(settings.device_secret_key.get_secret_value().encode()).digest()
    return AESGCM(key)


def new_device_secret() -> str:
    return secrets.token_hex(32)


def encrypt_device_secret(settings: Settings, band_id: str, secret: str) -> bytes:
    nonce = os.urandom(_NONCE_BYTES)
    return nonce + _aead(settings).encrypt(nonce, secret.encode(), band_id.encode())


def decrypt_device_secret(settings: Settings, band_id: str, ciphertext: bytes) -> str:
    nonce, body = ciphertext[:_NONCE_BYTES], ciphertext[_NONCE_BYTES:]
    return _aead(settings).decrypt(nonce, body, band_id.encode()).decode()


def sign_device_request(secret: str, timestamp: str, body: bytes) -> str:
    """What the band computes (also used by scripts/band_sim.py and tests)."""
    return hmac.new(secret.encode(), timestamp.encode() + body, hashlib.sha256).hexdigest()


def signature_valid(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    return hmac.compare_digest(sign_device_request(secret, timestamp, body), signature.lower())


def normalize_band_id(raw: str) -> str:
    """Same as the app's parseDeviceCode for a bare ID: trimmed, upper case."""
    return raw.strip().upper()
