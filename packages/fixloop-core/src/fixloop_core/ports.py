"""Ports: the small interfaces the loop is written against (design: "Interfaces to write first").

The loop (`fixloop_core.loop`) never imports Redis, SQLAlchemy, Docker or a model SDK. It talks to
these protocols; each app wires real adapters in its `main.py`, and tests wire fakes. An
import-linter contract keeps it that way.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from fixloop_core.config import TenantLimits
from fixloop_core.models import (
    ArtifactRef,
    AttemptRecord,
    BudgetLimits,
    ExecRequest,
    ExecResult,
    OutboxItem,
    TaskEvent,
    TaskResult,
    Usage,
)


@dataclass
class Need:
    """A budget reservation (or settlement): what one model call or execution may consume."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    sandbox_seconds: float = 0.0
    executions: int = 0

    def dims(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "in_tokens": self.input_tokens,
            "out_tokens": self.output_tokens,
            "cost": round(self.cost_usd * 1_000_000),
            "sandbox_ms": round(self.sandbox_seconds * 1000),
            "executions": self.executions,
        }


@dataclass(frozen=True)
class Claim:
    """Ownership of a task: every write is conditioned on `fence` still being current (ADR-21)."""

    task_id: uuid.UUID
    worker: str
    fence: int


@dataclass
class UploadInfo:
    upload_id: str
    key: str
    kind: str
    size: int | None


class TaskStore(Protocol):
    async def checkpoint(
        self,
        claim: Claim,
        *,
        phase: str,
        state: dict[str, Any],
        usage: dict[str, Any],
        snapshot_hash: str | None = None,
        env_hash: str | None = None,
        best_attempt_no: int | None = None,
    ) -> None: ...
    async def renew(self, claim: Claim, lease_seconds: int) -> None: ...
    async def record_attempt(self, claim: Claim, record: AttemptRecord) -> None: ...
    async def list_attempts(self, task_id: uuid.UUID) -> list[AttemptRecord]: ...
    async def record_execution(
        self,
        task_id: uuid.UUID,
        attempt_no: int | None,
        request: ExecRequest,
        result: ExecResult,
        summary: dict[str, Any] | None = None,
    ) -> None: ...
    async def record_llm_call(self, values: dict[str, Any]) -> None: ...
    async def record_artifact(
        self,
        *,
        tenant_id: uuid.UUID,
        task_id: uuid.UUID,
        kind: str,
        key: str,
        sha256: str,
        size: int,
        content_type: str,
    ) -> None: ...
    async def cancel_requested(self, task_id: uuid.UUID) -> bool: ...
    async def upload(self, upload_id: str, tenant_id: uuid.UUID) -> UploadInfo | None: ...
    async def failed_recently(self, tenant_id: uuid.UUID, spec_hash: str, exclude: uuid.UUID) -> bool: ...
    async def finalize(
        self,
        claim: Claim,
        *,
        tenant_id: uuid.UUID,
        result: TaskResult,
        usage: dict[str, Any],
        best_attempt_no: int | None,
        outbox: list[OutboxItem],
    ) -> TaskEvent | None: ...


class EventSink(Protocol):
    async def emit(
        self, task_id: uuid.UUID, type_: str, payload: dict[str, Any] | None = None
    ) -> TaskEvent: ...


class ExecPort(Protocol):
    async def run(
        self, request: ExecRequest, is_cancelled: Callable[[], Awaitable[bool]] | None = None
    ) -> ExecResult: ...


class BudgetPort(Protocol):
    async def start_task(self, task_id: str, usage: Usage) -> None: ...
    async def reserve(
        self, task_id: str, tenant_id: str, need: Need, task_limits: BudgetLimits, tenant_limits: TenantLimits
    ) -> str: ...
    async def settle(self, task_id: str, tenant_id: str, rid: str, actual: Need) -> None: ...
    async def release(self, task_id: str, tenant_id: str, rid: str) -> None: ...


class CachePort(Protocol):
    async def get(self, layer: str, key: str) -> Any: ...
    async def put(self, layer: str, key: str, value: Any) -> None: ...
    async def delete(self, key: str) -> None: ...


class GitHubReader(Protocol):
    async def tarball(self, installation_id: int, repo: str, sha: str, max_bytes: int) -> bytes: ...


class ObjectStore(Protocol):
    async def put_bytes(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> ArtifactRef: ...
    async def get_bytes(self, key: str, max_bytes: int | None = None) -> bytes: ...
    async def exists(self, key: str) -> bool: ...
    async def size(self, key: str) -> int | None: ...
    async def delete(self, key: str) -> None: ...
    async def delete_prefix(self, prefix: str) -> int: ...
    async def presign_get(self, key: str, ttl: int = 300) -> str: ...
    async def presign_put(self, key: str, content_type: str, ttl: int = 3600) -> str: ...


def task_prefix(tenant_id: str, *parts: str) -> str:
    return "/".join(["tenants", str(tenant_id), *parts])
