# Kala Dhaga — Backend

FastAPI + PostgreSQL + Redis + arq. See `CLAUDE.md` for the product rules and data model.

## Run everything

```bash
cp .env.example .env          # optional; defaults work for local dev
docker compose up --build     # api, worker, postgres, redis
```

- API: http://localhost:8000 — docs: http://localhost:8000/docs — health: http://localhost:8000/health
- Postgres: `localhost:5433` (user/password/db `kaladhaga`), test db `kaladhaga_test`
- Redis: `localhost:6380`
- Code changes reload the api and worker automatically.

## Develop on the host (optional)

Uses [uv](https://docs.astral.sh/uv/) (`pyproject.toml` + `uv.lock`).

```bash
uv sync                               # creates .venv with Python 3.12
docker compose up -d postgres redis   # dependencies only
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
uv run arq app.workers.settings.WorkerSettings
```

## Checks (run before every commit)

```bash
uv run ruff format . && uv run ruff check . && uv run mypy && uv run pytest
# or inside docker:
docker compose run --rm api pytest
```

Tests wipe and re-migrate `kaladhaga_test` once per run (never the dev database) and
always use the fake providers.

## Migrations

```bash
uv run alembic revision --autogenerate -m "add wearers"   # then review the file
uv run alembic upgrade head
```

## Auth (B1)

- Login: phone + password. A 4-digit OTP (5 min, 5 tries) verifies the number at sign-up and
  for password reset. In docker compose every OTP is `1234` (`OTP_FIXED_CODE`, local only);
  elsewhere the code is random and never logged.
- Access token: JWT, 15 min. Refresh token: opaque, 30 days, rotated on every use; a reused
  (already rotated) token ends that login on every device. Password reset ends all logins.
- Limits: OTP resend cooldown 30 s and 5 sends/hour per phone; 5 failed logins lock the
  number for 15 min. All are settings (`.env.example`).
- Passwords: Argon2id. OTPs, refresh and reset tokens are stored only as HMACs.

## Family, bands and contacts (B2)

```bash
docker compose exec api python -m scripts.seed_bands        # sold bands KD-0001..KD-0010
uv run python scripts/band_sim.py heartbeat KD-0001          # act as the band
uv run python scripts/band_sim.py pair KD-0001               # answer the app's vibrate check
```

- `seed_bands` writes each band's signing secret to `.dev-bands.json` (git-ignored), which
  `band_sim.py` reads. `--reset` gives the bands new secrets and unlinks them; `--unsold`
  creates bands that are not sold yet. Same thing over HTTP: `POST /dev/bands`.
- `/dev/*` (seed bands, read a band's SMS inbox) exists only when `APP_ENV` is local or test.
- Pairing over QR / typed code: the app calls `vibrate-check`, the backend texts the band
  `KD1 PAIR <nonce>`, the band pulses and calls `/device/v1/pair-ack`, the user confirms
  (`vibrate-check/confirm`), then `claim`. The claim's audit row records which proof existed.
- Band requests are signed (`X-Device-Id`, `X-Timestamp`, `X-Signature` = HMAC-SHA256 of
  timestamp + body), at most 5 minutes off, each signature accepted once. Band secrets are
  stored encrypted (AES-GCM, `DEVICE_SECRET_KEY`).
- Contact limits (5 per cohort, 2 guardians) are enforced by the service and again by a
  database trigger. A member is **ready** with a claimed band, a Cohort 1 contact and a guardian.
- Member photos: `STORAGE_PROVIDER=fake` keeps them under `MEDIA_DIR` (`var/media`, git-ignored)
  and the API returns signed links that expire (`MEDIA_URL_TTL_S`).

## Providers

`TELEPHONY_PROVIDER`, `SMS_PROVIDER`, `PUSH_PROVIDER`, `STORAGE_PROVIDER` = `fake` (default)
or the real one.
Fakes log what they would have done and keep it in memory for tests. `APP_ENV=test`
refuses any real provider.

## Logging

Every part of the system leaves a trace, and nothing sensitive is written down.

| Where | What |
|---|---|
| stdout | everything, readable (`LOG_FORMAT=console`) or JSON (`json`, use in staging/prod) |
| `logs/<service>.log` | JSON lines, everything at `LOG_LEVEL`+ (`service` = api, worker, migrate) |
| `logs/<service>.error.log` | JSON lines, WARNING and above, with tracebacks |
| `logs/audit-<service>.log` | JSON lines, audit events only |
| `audit_log` table | the audit trail as evidence: **append-only, enforced by the database** (UPDATE/DELETE/TRUNCATE are refused by a trigger) |

Files rotate daily at UTC midnight and are kept `LOG_RETENTION_DAYS` days (default 90).
In production, ship stdout to CloudWatch (or similar) with a longer retention.

- **Structured:** `log.info("alert.created", alert_id=..., mode="test")` — an event name plus
  fields, never values pasted into prose. Use `get_logger(__name__)` from `app.core.logging`.
- **Traceable:** each HTTP request gets a `request_id` (taken from `X-Request-ID` or generated,
  returned in the response header and in every error body). Every line logged while handling it
  carries it, and so does each audit row. Bind more with `bind_log_context(user_id=..., alert_id=...)`.
  Worker jobs carry `job_id` and `task`; pass `alert_id` along so an alert is traceable across
  api → worker → providers.
- **One access line per request:** `http.request` with method, path, status, duration, client ip.
- **Every job:** `job.started` / `job.succeeded` (duration) / `job.failed` (traceback).
  `worker_heartbeat` runs every minute, so the logs show the worker was alive at any moment.
- **Masked automatically** (`app/core/masking.py`, applied to every record, including
  third-party libraries): phone numbers → `+91******3210`; passwords, OTPs, codes, tokens,
  secrets, signatures, nonces → `***`; `lat`/`lng` rounded to ~1 km; `/a/<map token>` → `/a/***`;
  JWTs and Bearer tokens. SMS bodies also hide 4–8 digit codes.
- **Audit:** call `record_audit(session, action="device.claimed", actor_type="user", ...)` for
  anything that may be needed as proof later. It is saved in the same transaction as the change.
- All timestamps UTC, ISO 8601 with milliseconds.
