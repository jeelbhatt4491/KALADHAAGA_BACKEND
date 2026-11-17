"""Small Redis-backed limits (cooldowns, counters, lockouts). Keys contain a
hash of the phone, never the phone itself."""

from __future__ import annotations

import hashlib

from redis.asyncio import Redis


def phone_key(prefix: str, phone: str) -> str:
    return f"{prefix}:{hashlib.sha256(phone.encode()).hexdigest()[:24]}"


async def acquire_cooldown(redis: Redis, key: str, seconds: int) -> int:
    """Start a cooldown. Returns 0 if acquired, else the seconds left."""
    if await redis.set(key, "1", ex=seconds, nx=True):
        return 0
    return max(int(await redis.ttl(key)), 1)


async def hit_counter(redis: Redis, key: str, window_s: int) -> int:
    """Increment a counter that expires `window_s` after its first hit."""
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, window_s, nx=True)
        count, _ = await pipe.execute()
    return int(count)


async def counter_value(redis: Redis, key: str) -> int:
    value = await redis.get(key)
    return int(value) if value else 0


async def seconds_left(redis: Redis, key: str) -> int:
    return max(int(await redis.ttl(key)), 1)
