"""Process settings, read from environment variables (and `.env` in development).

Every variable starts with `FIXLOOP_` and nests with `__`, for example
`FIXLOOP_REDIS__QUEUE_URL`. Settings are validated at start: a missing or malformed value stops
the process with a clear message instead of failing on the first task.

Clients (Gemini, S3, Redis) are always built from these explicit values, never from ambient
environment variables, so the configuration you can read is the configuration that runs.
"""

from __future__ import annotations

import os
import socket
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def _find_up(name: str) -> Path | None:
    current = Path.cwd().resolve()
    for folder in (current, *current.parents):
        candidate = folder / name
        if candidate.exists():
            return candidate
    return None


def default_config_dir() -> Path:
    found = _find_up("config/models.yaml")
    return found.parent if found else Path("/app/config")


def default_prompts_dir() -> Path:
    found = _find_up("prompts/shared")
    return found.parent if found else Path("/app/prompts")


class DatabaseSettings(BaseModel):
    url: str = "postgresql+asyncpg://fixloop:fixloop@localhost:5432/fixloop"
    pool_size: int = 10
    echo: bool = False


class RedisSettings(BaseModel):
    # Two instances on purpose: queues and counters must never be evicted, caches should be.
    queue_url: str = "redis://localhost:6379/0"
    cache_url: str = "redis://localhost:6380/0"


class StorageSettings(BaseModel):
    backend: Literal["s3", "local"] = "s3"
    bucket: str = "fixloop-dev"
    endpoint_url: str | None = "http://localhost:9000"
    public_endpoint_url: str | None = None  # how clients reach the store for presigned URLs
    region: str = "us-east-1"
    access_key_id: SecretStr | None = None
    secret_access_key: SecretStr | None = None
    local_root: Path = Path(".fixloop-data/objects")


class GeminiSettings(BaseModel):
    backend: Literal["gemini", "scripted"] = "gemini"
    api_key: SecretStr | None = None
    vertexai: bool = False
    project: str | None = None
    location: str = "us-central1"
    scripted_path: Path | None = None


class GitHubSettings(BaseModel):
    enabled: bool = False
    app_id: int | None = None
    private_key: SecretStr | None = None
    private_key_path: Path | None = None
    webhook_secret: SecretStr | None = None
    api_url: str = "https://api.github.com"
    bot_login: str = "fixloop[bot]"


class RunnerSettings(BaseModel):
    backend: Literal["docker", "local"] = "docker"
    # The local backend runs code directly on the host with no isolation. It exists for tests
    # and for trying Fixloop on your own code; both flags must be set to use it.
    allow_unsafe_local: bool = False
    isolation: Literal["production", "dev"] = "dev"
    runtime: str | None = None  # "runsc" in production
    slots: int = 2
    runner_id: str = Field(default_factory=socket.gethostname)
    index_url: str = "http://pkgproxy:8080/simple"
    proxy_network: str = "fixloop-sandbox-proxy"
    require_pinned_images: bool = False
    image_overrides: dict[str, str] = Field(default_factory=dict)
    workdir: Path = Path(".fixloop-data/runner")
    local_pythons: dict[str, str] = Field(default_factory=dict)
    local_uid: int = 65534
    pg_bin_dir: str | None = None
    seccomp_profile: Path | None = None
    conformance_interval_s: int = 900
    docker_host: str | None = None
    env_ttl_days: int = 7
    keep_exec_dirs: bool = False  # local backend: keep workspaces for debugging
    drain_timeout_s: int = 120


class WorkerSettings(BaseModel):
    concurrency: int = 4
    worker_id: str = Field(default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}")
    lease_seconds: int = 60
    heartbeat_seconds: int = 20
    reconcile_interval_s: int = 60
    exec_wait_slack_s: int = 120


class ApiSettings(BaseModel):
    host: str = "0.0.0.0"  # noqa: S104 - the API container must listen on all interfaces
    port: int = 8080
    public_url: str = "http://localhost:8080"
    key_cache_ttl_s: int = 60
    upload_token_secret: SecretStr = SecretStr("dev-only-change-me")
    cors_origins: list[str] = Field(default_factory=list)


class PublisherSettings(BaseModel):
    poll_interval_s: float = 1.0
    webhook_timeout_s: float = 10.0
    allow_private_callbacks: bool = False  # tests only: lets callbacks target localhost


class ObservabilitySettings(BaseModel):
    log_level: str = "INFO"
    log_json: bool = True
    otlp_endpoint: str | None = None
    metrics_port: int | None = None


class FixloopSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FIXLOOP_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
    )

    env: Literal["local", "test", "staging", "production"] = "local"
    config_dir: Path = Field(default_factory=default_config_dir)
    prompts_dir: Path = Field(default_factory=default_prompts_dir)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    gemini: GeminiSettings = Field(default_factory=GeminiSettings)
    github: GitHubSettings = Field(default_factory=GitHubSettings)
    runner: RunnerSettings = Field(default_factory=RunnerSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    publisher: PublisherSettings = Field(default_factory=PublisherSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)

    def check_production_safety(self) -> list[str]:
        """Settings that are fine on a laptop but unacceptable in production."""
        problems: list[str] = []
        if self.env == "production":
            if self.runner.backend == "local" or self.runner.allow_unsafe_local:
                problems.append("runner.backend=local is not allowed in production")
            if self.runner.isolation != "production" or self.runner.runtime != "runsc":
                problems.append("production runners need isolation=production and runtime=runsc")
            if self.api.upload_token_secret.get_secret_value() == "dev-only-change-me":
                problems.append("api.upload_token_secret must be set")
            if self.gemini.backend != "gemini":
                problems.append("the scripted model backend is for tests only")
        return problems


@lru_cache(maxsize=1)
def get_settings() -> FixloopSettings:
    return FixloopSettings()
