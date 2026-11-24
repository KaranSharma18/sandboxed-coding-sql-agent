"""Response models of the public API (the OpenAPI document is generated from these)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from fixloop_core.storage import tables as t


class TaskOut(BaseModel):
    id: str
    kind: str
    status: str
    phase: str
    outcome: str | None = None
    priority: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested: bool = False
    usage: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    retry_of: str | None = None
    links: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def of(cls, task: t.Task, base_url: str = "") -> TaskOut:
        base = f"{base_url.rstrip('/')}/v1/tasks/{task.id}"
        return cls(
            id=str(task.id),
            kind=task.kind,
            status=task.status,
            phase=task.phase,
            outcome=task.outcome,
            priority=task.priority,
            created_at=task.created_at,
            started_at=task.started_at,
            finished_at=task.finished_at,
            cancel_requested=task.cancel_requested,
            usage=task.usage or {},
            result=task.result,
            retry_of=str(task.retry_of) if task.retry_of else None,
            links={
                "self": base,
                "events": f"{base}/events",
                "attempts": f"{base}/attempts",
                "patch": f"{base}/patch",
                "artifacts": f"{base}/artifacts",
            },
        )


class TaskList(BaseModel):
    items: list[TaskOut]
    next_cursor: str | None = None


class AttemptList(BaseModel):
    items: list[dict[str, Any]]


class ArtifactOut(BaseModel):
    id: str
    kind: str
    size_bytes: int
    content_type: str
    sha256: str
    created_at: datetime
    download_url: str


class UploadRequest(BaseModel):
    kind: str = Field(
        pattern="^(repo|hidden_tests|seed|expected_rows|parquet)$", description="what the upload is for"
    )
    content_type: str = Field(default="application/octet-stream", max_length=100)
    size_bytes: int = Field(gt=0)


class UploadOut(BaseModel):
    upload_id: str
    upload_url: str
    method: str = "PUT"
    headers: dict[str, str] = Field(default_factory=dict)
    expires_at: datetime


class UsageOut(BaseModel):
    days: list[dict[str, Any]]
    remaining_today_usd: float
