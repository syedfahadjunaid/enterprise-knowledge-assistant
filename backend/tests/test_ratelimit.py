"""Rate limiters.

The Redis limiter is tested against a real Redis (Testcontainers, the same image as Docker
Compose) rather than an in-process imitation: fake Redis libraries lag behind the real client
and broke when redis-py 8.2 came out.
"""

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import redis.asyncio as redis

from app.ratelimit import InMemoryRateLimiter, RedisRateLimiter


async def test_in_memory_limiter_allows_up_to_the_limit() -> None:
    limiter = InMemoryRateLimiter()
    results = [await limiter.hit("ask:bob", 3) for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]
    assert 1 <= results[-1].retry_after <= 60
    assert (await limiter.hit("ask:alice", 3)).allowed  # separate key, separate budget


@pytest.fixture(scope="module")
def redis_address() -> Iterator[tuple[str, int]]:
    try:
        from testcontainers.community.redis import RedisContainer
    except ImportError:  # older testcontainers layout
        from testcontainers.redis import RedisContainer  # type: ignore[no-redef]

    container = RedisContainer("redis:8.8-alpine")
    try:
        container.start()
    except Exception as e:  # noqa: BLE001 - any Docker problem means "can't run here"
        pytest.skip(f"Docker is not available: {e}")
    try:
        yield container.get_container_host_ip(), int(container.get_exposed_port(6379))
    finally:
        container.stop()


@pytest.fixture
async def redis_client(redis_address: tuple[str, int]) -> AsyncIterator[redis.Redis]:
    host, port = redis_address
    client = redis.Redis(host=host, port=port)
    yield client
    await client.aclose()


@pytest.mark.integration
async def test_redis_limiter_counts_per_key(redis_client: redis.Redis) -> None:
    limiter = RedisRateLimiter(redis_client)
    bob, carol = f"ask:bob-{uuid.uuid4()}", f"ask:carol-{uuid.uuid4()}"
    results = [await limiter.hit(bob, 2) for _ in range(3)]
    assert [r.allowed for r in results] == [True, True, False]
    assert 1 <= results[-1].retry_after <= 60
    assert (await limiter.hit(carol, 2)).allowed


@pytest.mark.integration
async def test_redis_keys_expire_with_their_window(redis_client: redis.Redis) -> None:
    limiter = RedisRateLimiter(redis_client)
    key = f"ask:dave-{uuid.uuid4()}"
    await limiter.hit(key, 5)
    [stored] = [k async for k in redis_client.scan_iter(match=f"ratelimit:{key}:*")]
    assert 0 < await redis_client.ttl(stored) <= 60  # no key outlives its window


async def test_redis_outage_fails_open() -> None:
    unreachable = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)
    result = await RedisRateLimiter(unreachable).hit("ask:bob", 1)
    assert result.allowed
    await unreachable.aclose()
