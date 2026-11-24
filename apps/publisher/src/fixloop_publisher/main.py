"""`fixloop-publisher`: the outbox dispatcher and GitHub token broker."""

from __future__ import annotations

import asyncio
import signal

from redis.asyncio import Redis

from fixloop_core.config import load_config
from fixloop_core.github import GitHubApp
from fixloop_core.objectstore import build_object_store
from fixloop_core.observability import configure_logging, start_metrics
from fixloop_core.settings import get_settings
from fixloop_core.storage.db import Database
from fixloop_publisher.service import PublisherService


async def _serve() -> None:
    settings = get_settings()
    configure_logging(settings.observability, "publisher")
    start_metrics(settings.observability.metrics_port)
    config = load_config(settings.config_dir)
    db = Database(settings.database)
    redis = Redis.from_url(settings.redis.queue_url, socket_timeout=30, health_check_interval=30)
    blobs = build_object_store(
        settings.storage, settings.api.public_url, settings.api.upload_token_secret.get_secret_value()
    )
    github = GitHubApp(settings.github) if settings.github.enabled else None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda *_: stop.set())
    try:
        await PublisherService(settings, config, db, redis, blobs, github).serve(stop)
    finally:
        if github is not None:
            await github.close()
        await db.close()
        await redis.aclose()


def run() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    run()
