# Kala Dhaga — Backend (Python / FastAPI)

## What this is
Kala Dhaga is a wrist-worn personal safety band (gold bangle, no screen). The band has its own SIM, GNSS, microphone and vibration motor. When the wearer squeezes the band or screams, the band sends an alert to **this backend**, which:
- calls the wearer's emergency contacts through Exotel (parallel calls, "press 1 to listen", conference with the band's mic),
- sends SMS with a live map link,
- vibrates linked family bands,
- streams live location to the app and the public map page,
- ends the alert only when two authorised people confirm.

The backend is the **centre of the system**. The band talks only to it; the Flutter app, the Shopify website and the live map page read from it. **An alert must work even if every phone involved is off** — the band + backend + telephony do the whole response.

Sibling projects (same parent folder):
- `../kaladhaga_app` — Flutter app. **Its CLAUDE.md is the source of truth for the API contract** (endpoint paths, request/response field names). The backend implements that contract; if something must change, say so explicitly so the app can be updated too.

## Product rules (must hold everywhere)
- **Account holder model.** One account holder (phone login) manages one or more **family members (wearers)**. Wearers often have no phone. Each band belongs to exactly one wearer; **one active band per wearer**.
- **No Aadhaar / DigiLocker in Phase 1.** Identity verification is postponed. Never store Aadhaar numbers.
- **Contacts:** per wearer, Cohort 1 and Cohort 2, up to 5 each, ordered. Up to 2 guardians (contacts who accepted the guardian role). For a family member's band, the account holder is automatically in Cohort 1 and a guardian; for the account holder's own band ("self"), they are not.
- **Band active** only when the wearer has ≥1 Cohort 1 contact and ≥1 guardian.
- **Family Circle:** all bands under one account are linked automatically (Family Alert vibration); bands from other accounts can be linked with approval.
- **Ending an alert:** needs **any two** of: the account holder and that wearer's guardians. Nothing the band sends can end an alert. A **test** can be ended by the account holder alone.
- **Test mode:** a 2-minute window; everything runs for real but every SMS/call/push says TEST; police features never run in test mode; limit 5 tests per band per month.
- **Police (Phase 1):** no automatic contact. The app and map page give contacts a Call 112 button and copyable details. SMS to 112 from the band is Phase 2.
- **Privacy:** no tracking outside an alert. Location is stored only during an alert (plus last-known position for the dead-zone fallback). Live map links expire when the alert ends.

## Stack
- Python 3.12, **FastAPI**, Pydantic v2, SQLAlchemy 2 (async) + Alembic
- **PostgreSQL** (AWS RDS in production)
- **Redis** for live state, pub/sub and the job queue
- Background worker: **arq** (async Redis queue) for the alert engine timers and outbound calls/SMS
- Auth: phone + **password** login; a 4-digit OTP only verifies the number at sign-up and for password reset (as in the app contract). JWT access + refresh tokens
- Packaging: **uv** (`pyproject.toml` + `uv.lock`)
- Integrations behind interfaces, each with a **Fake** implementation used in development and tests:
  - `TelephonyProvider` → Exotel (calls, IVR digit "press 1", conference)
  - `SmsProvider` → Exotel SMS (DLT templates)
  - `PushProvider` → Firebase Cloud Messaging
  - `GeocodingProvider` → address from coordinates (later)
  Switch with env vars; **never call a real provider from tests**.
- Hosting (later): AWS ap-south-1 (Mumbai), ECS Fargate or EC2, ElastiCache, S3 for photos (server-side encryption)
- Local development: **Docker Compose** (api, worker, postgres, redis) so it runs the same on Windows and Mac.

## Folder structure
```
app/
  main.py            # FastAPI app, routers mounted here
  core/              # settings, security (JWT, HMAC), logging, errors
  db/                # SQLAlchemy models, session, Alembic migrations
  api/
    auth.py  me.py  family.py  devices.py  contacts.py  invitations.py
    alerts.py  test_mode.py  ws.py
    device_api.py    # endpoints the band calls (/device/v1/...)
    webhooks.py      # Shopify + Exotel callbacks
    public.py        # live map page /a/{token}
    admin.py         # packing page
  services/          # business logic (alert engine, contacts rules, pairing)
  providers/         # telephony, sms, push, geocoding — interface + real + fake
  workers/           # arq tasks: call rounds, timers, notifications
tests/
scripts/
  band_sim.py        # band simulator: signs requests like a real band
docker-compose.yml
```

## Data model (starting point — keep in sync with the app contract)
- `users` — id, phone (unique, E.164), name, password_hash, phone_verified_at, created_at, updated_at
- `otp_codes` — phone, purpose (signup/reset_password), code_hash (4 digits), expires_at, attempts (5-minute expiry, max 5 attempts)
- `audit_log` — append-only (DB trigger blocks UPDATE/DELETE/TRUNCATE): occurred_at, actor_type, actor_id, action, entity_type, entity_id, request_id, ip, details JSON (masked)
- `wearers` — id, account_id → users, name, relationship, age, gender, photo_s3_key (optional), consent_at, is_self
- `orders` / `order_items` — from the Shopify webhook; order_items has wearer_name, relationship, device_id (set at packing)
- `devices` — id, qr_code / band_id (unique, e.g. KD-7F3A9C21), imei, sim_msisdn, device_secret_hash, status (`unsold` → `sold` → `active` → `deactivated`), wearer_id, firmware, battery_pct, last_seen_at
  - **partial unique index: one active device per wearer**
- `contacts` — id, wearer_id, name, phone, relationship, cohort (1/2), priority, area (city/area text), proximity answers, vicinity_score, is_guardian, contact_user_id, invitation status
- `family_circles` / `family_circle_members`
- `alerts` — id, device_id, wearer_id, mode (real/test), trigger (squeeze/scream), status, started_at, ended_at, map_token
- `alert_end_confirmations` — alert_id, user_id, confirmed_at
- `alert_locations` — alert_id, lat, lon, accuracy_m, source, recorded_at
- `alert_calls` — alert_id, contact_id, round, provider_call_id, status (queued/ringing/joined/no_answer), answered_at, joined_at
- `alert_events` — alert_id, type, payload JSON, created_at (timeline)
- `notification_preferences` — per user (emergency alerts always on)

## Alert engine (summary)
On `POST /device/v1/alert`: verify signature → de-duplicate open alert → create alert + first location + map token → respond within 1 s with `{alert_id, mode, conference_number}` → enqueue in parallel: family alert SMS to linked bands, SMS with map link to all contacts, push to app users, call round for Cohort 1 after a short hold window (start 10 s).
Call rounds: ring the whole cohort at once; IVR "press 1 to listen"; on 1 → join the conference, mark joined, send the band a "connected" command on the first join. No join within the round timeout (start 40 s) → next cohort; alternate 1 → 2 → 1 until someone joins; once someone joined, no new rounds.
Locations every 5–10 s → saved and published on Redis `alert:{id}` → WebSocket + map page.
Ending: two confirmations as per the rules above → hang up, close conference, send band "ended", expire map token, "safe" SMS + push.
All timings are config values.

## Band ↔ backend contract
- HTTPS, JSON. Headers `X-Device-Id`, `X-Timestamp`, `X-Signature` = HMAC-SHA256(device secret, timestamp + body); reject timestamps more than 5 minutes off.
- `POST /device/v1/alert`, `/location`, `/heartbeat`, `/pair-ack`; `GET /device/v1/alert/{id}/state`.
- Backend → band commands by SMS from our fixed number: `KD1 PAIR <nonce>`, `KD1 FAM <alert_id>`, `KD1 CONN <alert_id>`, `KD1 END <alert_id>`.
- Field names are proposals until the firmware side confirms them; mark them TODO where unconfirmed.

## Rules for code
- Every endpoint has request/response Pydantic models and tests.
- Business rules (one active band, two-person end, guardian limits, cohort limits, test limits) are enforced in services **and** where possible in the database.
- Webhooks (Shopify, Exotel) verify signatures and are idempotent.
- Never log OTPs, tokens, device secrets, full phone numbers or exact locations; mask them.
- **Logging (see README "Logging"):** use `get_logger(__name__)` and log structured events (`log.info("alert.created", alert_id=...)`), never values pasted into text. Every request, job, provider call, state change and failure is logged; `app/core/masking.py` masks every record automatically, but don't rely on it as an excuse to log secrets. Bind `user_id` / `alert_id` / `device_id` to the log context so one alert is traceable across api, worker and providers. Anything that may be needed as evidence (auth, device claim/deactivate, contacts/guardians, alert start/confirm/end, tests, admin, webhooks) also goes through `record_audit` in the same transaction.
- Secrets only from environment variables / `.env` (git-ignored). Provide `.env.example`.
- Time in UTC in the database; ISO 8601 in the API.
- Phone numbers stored in E.164 (+91…).
- Keep changes small, run ruff + mypy + pytest before committing, and explain what changed after each task.
