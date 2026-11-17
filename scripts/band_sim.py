"""Band simulator: talks to the backend exactly like a band, signing each request.

    uv run python scripts/band_sim.py heartbeat KD-0001 --battery 82 --signal good
    uv run python scripts/band_sim.py inbox KD-0001          # SMS the backend sent the band
    uv run python scripts/band_sim.py pair KD-0001           # answer the latest KD1 PAIR
    uv run python scripts/band_sim.py pair-ack KD-0001 --nonce 1A2B3C4D

The band's secret comes from `.dev-bands.json` (written by `scripts/seed_bands.py`)
or --secret. --api defaults to $KD_API or http://localhost:8000. `inbox` and
`pair` read the band's SMS through /dev (local backend only); a real band gets
them on its SIM.

Signing (see app/core/device_auth.py; TODO(firmware): confirm):
    X-Device-Id, X-Timestamp (unix s), X-Signature = hex HMAC-SHA256(secret, timestamp + body)
Deliberately self-contained (no app imports): it is what firmware has to do.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

SECRETS_FILE = Path(__file__).resolve().parent.parent / ".dev-bands.json"


def _secret(band_id: str, given: str | None) -> str:
    if given:
        return given
    if SECRETS_FILE.exists():
        entry = json.loads(SECRETS_FILE.read_text(encoding="utf-8")).get(band_id)
        if entry and entry.get("secret"):
            return str(entry["secret"])
    sys.exit(f"no secret for {band_id}: run scripts/seed_bands.py (or pass --secret)")


def signed_post(
    api: str, band_id: str, secret: str, path: str, payload: dict[str, Any]
) -> httpx.Response:
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(secret.encode(), timestamp.encode() + body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Device-Id": band_id,
        "X-Timestamp": timestamp,
        "X-Signature": signature,
    }
    return httpx.post(f"{api}{path}", content=body, headers=headers, timeout=10)


def _inbox(api: str, band_id: str) -> list[str]:
    response = httpx.get(f"{api}/dev/bands/{band_id}/sms", timeout=10)
    response.raise_for_status()
    return [m["body"] for m in response.json()]


def _show(response: httpx.Response) -> None:
    print(response.status_code, response.text or "(no body)")
    if response.status_code >= 400:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("command", choices=["heartbeat", "inbox", "pair", "pair-ack"])
    parser.add_argument("band_id", type=str.upper)
    parser.add_argument("--api", default=os.environ.get("KD_API", "http://localhost:8000"))
    parser.add_argument("--secret")
    parser.add_argument("--battery", type=int, default=87)
    parser.add_argument("--signal", choices=["good", "fair", "poor"], default="good")
    parser.add_argument("--firmware", default="0.9.0-sim")
    parser.add_argument("--nonce")
    args = parser.parse_args()
    api = args.api.rstrip("/")

    if args.command == "inbox":
        for body in _inbox(api, args.band_id):
            print(body)
        return

    secret = _secret(args.band_id, args.secret)
    if args.command == "heartbeat":
        payload = {
            "battery_percent": args.battery,
            "signal": args.signal,
            "firmware_version": args.firmware,
        }
        _show(signed_post(api, args.band_id, secret, "/device/v1/heartbeat", payload))
        return

    nonce = args.nonce
    if args.command == "pair":
        pairs = [b for b in _inbox(api, args.band_id) if b.startswith("KD1 PAIR ")]
        if not pairs:
            sys.exit("no KD1 PAIR in the band's inbox: tap the vibrate check in the app first")
        nonce = pairs[-1].split()[-1]
        print("bzzz (pulsing), acknowledging", pairs[-1].split()[0:2])
    if not nonce:
        sys.exit("--nonce is required")
    _show(signed_post(api, args.band_id, secret, "/device/v1/pair-ack", {"nonce": nonce}))


if __name__ == "__main__":
    main()
