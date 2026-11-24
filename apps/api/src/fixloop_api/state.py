"""Process-wide resources of the API, created once in the app's lifespan."""

from __future__ import annotations

from dataclasses import dataclass, field

from redis.asyncio import Redis

from fixloop_core.budget import BudgetLedger
from fixloop_core.config import AppConfig, load_config
from fixloop_core.events import EventBus
from fixloop_core.objectstore import ObjectStore, build_object_store
from fixloop_core.settings import FixloopSettings
from fixloop_core.storage.db import Database


@dataclass
class ApiState:
    settings: FixloopSettings
    config: AppConfig
    db: Database
    redis: Redis
    blobs: ObjectStore
    events: EventBus
    ledger: BudgetLedger
    key_cache: dict[str, tuple[float, object]] = field(default_factory=dict)

    @classmethod
    def build(cls, settings: FixloopSettings) -> ApiState:
        config = load_config(settings.config_dir)
        db = Database(settings.database)
        redis = Redis.from_url(settings.redis.queue_url, socket_timeout=30, health_check_interval=30)
        blobs = build_object_store(
            settings.storage, settings.api.public_url, settings.api.upload_token_secret.get_secret_value()
        )
        return cls(
            settings=settings,
            config=config,
            db=db,
            redis=redis,
            blobs=blobs,
            events=EventBus(db, redis),
            ledger=BudgetLedger(redis, config.limits.global_.daily_cost_usd),
        )

    async def close(self) -> None:
        await self.db.close()
        await self.redis.aclose()
