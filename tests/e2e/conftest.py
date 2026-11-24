"""End-to-end fixtures: Postgres, Redis, a local runner service and a worker with a scripted model."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest_asyncio

from fixloop_core.llm.scripted import ScriptedBackend
from fixloop_core.objectstore import LocalObjectStore
from fixloop_core.settings import (
    DatabaseSettings,
    FixloopSettings,
    GeminiSettings,
    RedisSettings,
    StorageSettings,
    WorkerSettings,
)
from fixloop_core.storage import repo
from fixloop_runner.backends.local import LocalBackend
from fixloop_runner.service import RunnerService
from fixloop_worker.service import WorkerService
from fixloop_worker.wiring import Resources, build_resources
from tests.integration.runner_helpers import CONFIG, runner_settings, shared_tmp


@dataclass
class Stack:
    resources: Resources
    backend: ScriptedBackend
    tenant: object
    worker: WorkerService

    def script(self, steps) -> ScriptedBackend:
        backend = ScriptedBackend(steps=list(steps))
        self.resources.deps.llm.backend = backend
        self.backend = backend
        return backend


@pytest_asyncio.fixture
async def stack(services, db) -> AsyncIterator[Stack]:
    workdir, objects = shared_tmp("fixloop-e2e-run-"), shared_tmp("fixloop-e2e-obj-")
    settings = FixloopSettings(
        env="test",
        database=DatabaseSettings(url=services.database_url, pool_size=10),
        redis=RedisSettings(queue_url=services.redis_url, cache_url=services.redis_cache_url),
        storage=StorageSettings(backend="local", local_root=objects),
        gemini=GeminiSettings(backend="scripted"),
        runner=runner_settings(workdir),
        worker=WorkerSettings(concurrency=2, lease_seconds=30, heartbeat_seconds=5, worker_id="e2e-worker"),
    )
    backend = ScriptedBackend()
    resources = build_resources(settings, backend=backend)
    await resources.redis.flushdb()
    await resources.cache_redis.flushdb()
    runner = RunnerService(
        settings, CONFIG, LocalBackend(settings.runner), LocalObjectStore(objects), resources.redis
    )
    worker = WorkerService(resources)
    stop = asyncio.Event()
    serving = [asyncio.create_task(runner.serve(stop)), asyncio.create_task(worker.serve(stop))]
    async with resources.db.transaction() as s:
        tenant = await repo.create_tenant(s, "e2e", plan="local", is_local=True)
    try:
        yield Stack(resources, backend, tenant, worker)
    finally:
        stop.set()
        await asyncio.wait(serving, timeout=60)
        await resources.close()
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(objects, ignore_errors=True)
