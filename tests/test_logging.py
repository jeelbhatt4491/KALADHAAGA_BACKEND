"""Log files: JSON lines, masked, with context, audit split into its own file."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, get_settings
from app.core.context import log_context
from app.core.logging import AUDIT_LOGGER, configure_logging, get_logger


@pytest.fixture
def log_dir(tmp_path: Path) -> Iterator[Path]:
    settings = Settings(log_to_file=True, log_dir=tmp_path, log_format="json")
    configure_logging(settings, service="api")
    yield tmp_path
    configure_logging(get_settings(), service="api")  # closes the file handlers


def _lines(path: Path) -> list[dict[str, Any]]:
    for handler in logging.getLogger().handlers:
        handler.flush()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_log_lines_are_json_masked_and_carry_context(log_dir: Path) -> None:
    log = get_logger("kaladhaga.test")
    with log_context(request_id="req-1", alert_id="al-9"):
        log.info("otp.sent", phone="+919876543210", otp="4821", lat=23.022505, purpose="signup")

    [line] = [x for x in _lines(log_dir / "api.log") if x["event"] == "otp.sent"]
    assert line["level"] == "INFO"
    assert line["service"] == "api" and line["env"] == "test"
    assert line["request_id"] == "req-1" and line["alert_id"] == "al-9"
    assert line["data"] == {
        "phone": "+91******3210",
        "otp": "***",
        "lat": 23.02,
        "purpose": "signup",
    }
    assert line["ts"].endswith("Z")
    raw = (log_dir / "api.log").read_text(encoding="utf-8")
    assert "9876543210" not in raw and "4821" not in raw


def test_plain_stdlib_logging_is_masked_too(log_dir: Path) -> None:
    logging.getLogger("some.library").warning("sent to %s", "+919876543210")

    [line] = [x for x in _lines(log_dir / "api.log") if x["logger"] == "some.library"]
    assert line["event"] == "sent to +91******3210"


def test_errors_get_their_own_file_with_traceback(log_dir: Path) -> None:
    log = get_logger("kaladhaga.test")
    try:
        raise ValueError("boom for +919876543210")
    except ValueError:
        log.exception("thing.failed")

    [line] = _lines(log_dir / "api.error.log")
    assert line["event"] == "thing.failed"
    assert line["exc_type"] == "ValueError"
    assert "Traceback" in line["exc"] and "9876543210" not in line["exc"]


def test_audit_events_go_only_to_audit_file(log_dir: Path) -> None:
    get_logger(AUDIT_LOGGER).info("device.claimed", device_id="KD-7F3A9C21")

    audit = _lines(log_dir / "audit-api.log")
    assert [x["event"] for x in audit] == ["device.claimed"]
    assert all(x["event"] != "device.claimed" for x in _lines(log_dir / "api.log"))


async def test_each_request_writes_one_access_line(app: FastAPI, log_dir: Path) -> None:
    # `app` first: creating it reconfigures logging, then log_dir points it at files.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        await http.get("/a/SecretMapToken123", headers={"X-Request-ID": "req-access-01"})

    lines = [x for x in _lines(log_dir / "api.log") if x["event"] == "http.request"]
    [line] = [x for x in lines if x.get("request_id") == "req-access-01"]
    assert line["data"]["status"] == 404
    assert line["data"]["path"] == "/a/***"
    assert line["level"] == "WARNING"


async def test_band_pairing_leaves_no_secrets_in_logs(
    app: FastAPI, client: AsyncClient, log_dir: Path
) -> None:
    from tests.helpers import account_holder, add_contact, add_member, band_post, seed_band

    auth = await account_holder(client, app)
    member = await add_member(client, auth)
    contact_phone = "+919812345678"
    await add_contact(client, auth, member["id"], phone=contact_phone)
    band = await seed_band(app)
    await client.post(f"/devices/{band['band_id']}/vibrate-check", headers=auth)
    [pair] = [m for m in app.state.providers.sms.sent if m.to == band["sim_msisdn"]]
    nonce = pair.body.split()[-1]
    await band_post(client, band, "/device/v1/pair-ack", {"nonce": nonce})

    _lines(log_dir / "api.log")  # flush
    logged = (log_dir / "api.log").read_text(encoding="utf-8") + (
        log_dir / "audit-api.log"
    ).read_text(encoding="utf-8")
    assert "device.pair_acked" in logged and "KD1 PAIR ***" in logged
    for secret in (nonce, band["secret"], contact_phone, band["sim_msisdn"]):
        assert secret not in logged, secret
