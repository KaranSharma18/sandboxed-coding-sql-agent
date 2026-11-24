"""Shared fixtures. Integration fixtures start real Postgres and Redis (see tests/services.py)."""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio

from tests.services import Services, migrate, pg_bin, start_services


def _services_available() -> bool:
    import os

    if os.environ.get("FIXLOOP_TEST_DATABASE_URL") and os.environ.get("FIXLOOP_TEST_REDIS_URL"):
        return True
    return pg_bin() is not None and shutil.which("redis-server") is not None


@pytest.fixture(scope="session")
def services() -> Iterator[Services]:
    if not _services_available():
        pytest.skip("Postgres/Redis not available (set FIXLOOP_TEST_DATABASE_URL and FIXLOOP_TEST_REDIS_URL)")
    started = start_services()
    try:
        migrate(started.database_url)
        yield started
    finally:
        started.stop()


@pytest_asyncio.fixture
async def db(services: Services) -> AsyncIterator:
    from sqlalchemy import text

    from fixloop_core.settings import DatabaseSettings
    from fixloop_core.storage.db import Database

    database = Database(DatabaseSettings(url=services.database_url, pool_size=5))
    async with database.transaction() as session:
        await session.execute(
            text(
                "TRUNCATE tenants, api_keys, tasks, attempts, executions, llm_calls, events, artifacts, uploads, "
                "github_installations, outbox, inbound_deliveries, audit_log CASCADE"
            )
        )
    yield database
    await database.close()


@pytest_asyncio.fixture
async def redis_queue(services: Services) -> AsyncIterator:
    from redis.asyncio import Redis

    client = Redis.from_url(services.redis_url, socket_timeout=30)
    await client.flushdb()
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def redis_cache(services: Services) -> AsyncIterator:
    from redis.asyncio import Redis

    client = Redis.from_url(services.redis_cache_url, socket_timeout=30)
    await client.flushdb()
    yield client
    await client.aclose()
