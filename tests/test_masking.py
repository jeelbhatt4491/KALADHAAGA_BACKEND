from __future__ import annotations

import pytest

from app.core.masking import mask_mapping, mask_phone, mask_sms_body, mask_text


@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        ("+919876543210", "+91******3210"),
        ("9876543210", "******3210"),
        ("+14155550123", "+*******0123"),
    ],
)
def test_mask_phone(raw: str, masked: str) -> None:
    assert mask_phone(raw) == masked


def test_mask_text_hides_phones_tokens_and_map_links() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc_DEF-123"
    text = f"call +919876543210 or 9123456789, see https://kd.in/a/Zx81kLmQ9p, Bearer {jwt}"

    out = mask_text(text)

    assert "9876543210" not in out and "+91******3210" in out
    assert "9123456789" not in out
    assert "Zx81kLmQ9p" not in out and "/a/***" in out
    assert jwt not in out


def test_mask_text_leaves_ids_and_timestamps_alone() -> None:
    text = "alert 3f2b9c1e-0d4a-4b7e-9a61-1c2d3e4f5a6b at 1731830400 battery 87"
    assert mask_text(text) == text


def test_mask_mapping_by_key() -> None:
    data = {
        "password": "kala1234",
        "otp": "1234",
        "code": "5678",
        "refresh_token": "abc",
        "device_secret": "s3cr3t",
        "x_signature": "deadbeef",
        "sim_msisdn": "+919876543210",
        "lat": 23.022505,
        "lng": 72.571365,
        "code_length": 4,
        "token_type": "bearer",
        "nested": {"to_phone": "+919876543210", "items": ["+918888777766"]},
    }

    out = mask_mapping(data)

    for key in ("password", "otp", "code", "refresh_token", "device_secret", "x_signature"):
        assert out[key] == "***", key
    assert out["sim_msisdn"] == "+91******3210"
    assert out["lat"] == 23.02 and out["lng"] == 72.57
    assert out["code_length"] == 4 and out["token_type"] == "bearer"
    assert out["nested"] == {"to_phone": "+91******3210", "items": ["+91******7766"]}


def test_mask_sms_body_hides_otp_and_pair_nonce() -> None:
    assert mask_sms_body("Your Kala Dhaga code is 4821. Valid 5 min.") == (
        "Your Kala Dhaga code is ***. Valid 5 min."
    )
    assert mask_sms_body("KD1 PAIR 9f8e7d6c") == "KD1 PAIR ***"
