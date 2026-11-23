"""Per-user rate limits (fixed one-minute windows), stored in Redis so they hold across API
replicas.

The limits protect cost and capacity (every question may become a paid LLM call), not access
control. If Redis is down we therefore *allow* the request and log an error: an outage of the
limiter shouldn't take the whole assistant down. Access control never depends on Redis.
"""

import logging
import time
from dataclasses import dataclass
from typing import Protocol

import redis.asyncio as redis

log = logging.getLogger(__name__)

WINDOW_SECONDS = 60


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after: int  # seconds until the window resets (only meaningful when not allowed)


class RateLimiter(Protocol):
    async def hit(self, key: str, limit: int) -> RateLimitResult: ...


class RedisRateLimiter:
    def __init__(self, client: redis.Redis) -> None:
        self._redis = client

    async def hit(self, key: str, limit: int) -> RateLimitResult:
        now = time.time()
        window = int(now // WINDOW_SECONDS)
        redis_key = f"ratelimit:{key}:{window}"
        try:
            # INCR and EXPIRE in one round trip; the key disappears after its window.
            async with self._redis.pipeline(transaction=True) as pipe:
                count, _ = await pipe.incr(redis_key).expire(redis_key, WINDOW_SECONDS).execute()
        except redis.RedisError as e:
            log.error("Rate limiter unavailable, allowing request: %s", e)
            return RateLimitResult(True, 0)
        retry_after = max(1, int((window + 1) * WINDOW_SECONDS - now))
        return RateLimitResult(int(count) <= limit, retry_after)


class InMemoryRateLimiter:
    """Same behaviour in one process. For tests and for running without Redis."""

    def __init__(self) -> None:
        self._counts: dict[tuple[str, int], int] = {}

    async def hit(self, key: str, limit: int) -> RateLimitResult:
        now = time.time()
        window = int(now // WINDOW_SECONDS)
        self._counts = {k: v for k, v in self._counts.items() if k[1] == window}
        count = self._counts.get((key, window), 0) + 1
        self._counts[(key, window)] = count
        return RateLimitResult(count <= limit, max(1, int((window + 1) * WINDOW_SECONDS - now)))
