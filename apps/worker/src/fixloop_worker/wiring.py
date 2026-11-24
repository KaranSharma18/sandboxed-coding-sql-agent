"""Building the loop's dependencies from settings (the only place adapters are chosen)."""

from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis

from fixloop_core.budget import BudgetLedger
from fixloop_core.cache import Cache
from fixloop_core.config import AppConfig, load_config
from fixloop_core.events import EventBus
from fixloop_core.execclient import ExecClient
from fixloop_core.github_source import GitHubSourceClient
from fixloop_core.llm.resilience import Breakers, RateLimiter
from fixloop_core.llm.service import LLMService
from fixloop_core.loop.runtime import LoopDeps
from fixloop_core.objectstore import build_object_store
from fixloop_core.prompts import get_library
from fixloop_core.settings import FixloopSettings
from fixloop_core.storage.db import Database
from fixloop_core.storage.taskstore import PgTaskStore


@dataclass
class Resources:
    settings: FixloopSettings
    config: AppConfig
    db: Database
    redis: Redis
    cache_redis: Redis
    deps: LoopDeps

    async def close(self) -> None:
        await self.db.close()
        await self.redis.aclose()
        await self.cache_redis.aclose()


def build_model_backend(settings: FixloopSettings):
    if settings.gemini.backend == "scripted":
        from fixloop_core.llm.scripted import ScriptedBackend

        if settings.gemini.scripted_path is None:
            raise SystemExit(
                "FIXLOOP_GEMINI__BACKEND=scripted needs FIXLOOP_GEMINI__SCRIPTED_PATH (a YAML script)"
            )
        return ScriptedBackend.from_yaml(settings.gemini.scripted_path)
    from fixloop_core.llm.gemini import GeminiBackend

    return GeminiBackend(settings.gemini)


def build_resources(settings: FixloopSettings, *, backend=None) -> Resources:
    config = load_config(settings.config_dir)
    config.models.validate_prices()
    prompts = get_library(settings.prompts_dir)
    db = Database(settings.database)
    redis = Redis.from_url(settings.redis.queue_url, socket_timeout=60, health_check_interval=30)
    cache_redis = Redis.from_url(settings.redis.cache_url, socket_timeout=10, health_check_interval=30)
    events = EventBus(db, redis)
    ledger = BudgetLedger(redis, config.limits.global_.daily_cost_usd)
    cache = Cache(cache_redis, redis)
    llm = LLMService(
        config=config,
        backend=backend or build_model_backend(settings),
        ledger=ledger,
        cache=cache,
        rate_limiter=RateLimiter(redis, config.models.rate_limits),
        breakers=Breakers(config.models.breaker),
    )
    blobs = build_object_store(
        settings.storage, settings.api.public_url, settings.api.upload_token_secret.get_secret_value()
    )
    github = GitHubSourceClient(redis, settings.github.api_url) if settings.github.enabled else None
    deps = LoopDeps(
        config=config,
        prompts=prompts,
        store=PgTaskStore(db, events),
        events=events,
        blobs=blobs,
        llm=llm,
        execs=ExecClient(redis, slack_s=settings.worker.exec_wait_slack_s),
        ledger=ledger,
        cache=cache,
        github=github,
        worker_id=settings.worker.worker_id,
        public_url=settings.api.public_url,
    )
    return Resources(settings, config, db, redis, cache_redis, deps)
