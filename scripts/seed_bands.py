"""Create sold bands for local testing (KD-0001 ... KD-0010 by default).

    docker compose exec api python -m scripts.seed_bands            # KD-0001..KD-0010
    docker compose exec api python -m scripts.seed_bands --start 11 --count 5
    docker compose exec api python -m scripts.seed_bands --reset    # new secrets, unlinked
    uv run python -m scripts.seed_bands --unsold --start 900 --count 1   # a band not sold yet

Each new band gets a random signing secret; the secrets are written to
`.dev-bands.json` (git-ignored) so `scripts/band_sim.py` can sign as that band.
Existing bands are left alone unless --reset. Refuses to run in staging/production.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import create_engine, create_session_factory
from app.services.devices import seed_bands

SECRETS_FILE = Path(__file__).resolve().parent.parent / ".dev-bands.json"


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--prefix", default="KD-")
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--reset", action="store_true", help="new secret, back to sold, unlinked")
    parser.add_argument("--unsold", action="store_true", help="create them as not sold yet")
    args = parser.parse_args()

    settings = get_settings()
    if settings.is_deployed:
        raise SystemExit("seed_bands is for local development only")
    configure_logging(settings, service="seed")
    engine = create_engine(settings)
    ids = [f"{args.prefix}{n:04d}" for n in range(args.start, args.start + args.count)]
    try:
        async with create_session_factory(engine)() as session:
            bands = await seed_bands(
                session, settings, ids, reset=args.reset, status="unsold" if args.unsold else "sold"
            )
    finally:
        await engine.dispose()

    known: dict[str, dict[str, str | None]] = {}
    if SECRETS_FILE.exists():
        known = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
    for band in bands:
        if band.secret is not None:
            known[band.band_id] = {"secret": band.secret, "sim_msisdn": band.sim_msisdn}
    SECRETS_FILE.write_text(json.dumps(known, indent=2, sort_keys=True), encoding="utf-8")

    for band in bands:
        state = "created/reset" if band.secret else "already there"
        print(f"{band.band_id}  {band.status:<7}  sim {band.sim_msisdn}  ({state})")
    print(f"secrets: {SECRETS_FILE.name}")


if __name__ == "__main__":
    asyncio.run(main())
