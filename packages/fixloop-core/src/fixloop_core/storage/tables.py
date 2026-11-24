"""Postgres schema (SQLAlchemy 2.0 declarative). Postgres is the system of record.

Everything needed to explain, bill, resume or audit a task lives here. Ids are UUIDv7 so they
sort by time. The `fence` column on tasks is a fencing token: every worker write includes
`WHERE fence = :mine`, so a worker that lost its lease can never overwrite newer state.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from fixloop_core.ids import uuid7


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[str]: ARRAY(Text)}


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200), unique=True)
    plan: Mapped[str] = mapped_column(String(40), default="standard")
    status: Mapped[str] = mapped_column(String(20), default="active")
    is_local: Mapped[bool] = mapped_column(Boolean, default=False)
    webhook_secret: Mapped[str] = mapped_column(String(128))
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    allowlist_extra: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    created_at: Mapped[datetime] = _now()


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    secret_hash: Mapped[bytes] = mapped_column(LargeBinary(32))
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    created_at: Mapped[datetime] = _now()
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_tasks_idempotency"),
        Index("ix_tasks_tenant_created", "tenant_id", "created_at"),
        Index("ix_tasks_status", "status"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="queued")
    outcome: Mapped[str | None] = mapped_column(String(32))
    phase: Mapped[str] = mapped_column(String(32), default="queued")
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB)
    spec_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    priority: Mapped[str] = mapped_column(String(16), default="interactive")
    snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    env_hash: Mapped[str | None] = mapped_column(String(64))
    budget: Mapped[dict[str, Any]] = mapped_column(JSONB)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    best_attempt_no: Mapped[int | None] = mapped_column(Integer)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fence: Mapped[int] = mapped_column(BigInteger, default=0)
    deliveries: Mapped[int] = mapped_column(Integer, default=0)
    retry_of: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    github: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Attempt(Base):
    __tablename__ = "attempts"
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True)
    attempt_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    record: Mapped[dict[str, Any]] = mapped_column(JSONB)
    fingerprint: Mapped[str] = mapped_column(String(64), default="")
    failure_class: Mapped[str | None] = mapped_column(String(40))
    failure_signature: Mapped[str] = mapped_column(String(64), default="")
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0)
    created_at: Mapped[datetime] = _now()


class Execution(Base):
    __tablename__ = "executions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    attempt_no: Mapped[int | None] = mapped_column(Integer)
    purpose: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    sandbox_seconds: Mapped[float] = mapped_column(Float, default=0)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    runner_id: Mapped[str] = mapped_column(String(200), default="")
    image_digest: Mapped[str] = mapped_column(String(200), default="")
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _now()


class LlmCall(Base):
    __tablename__ = "llm_calls"
    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    attempt_no: Mapped[int | None] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(100))
    fallback_from: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(20))
    schema_version: Mapped[str] = mapped_column(String(20))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    thinking_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    finish_reason: Mapped[str] = mapped_column(String(40), default="")
    reask_no: Mapped[int] = mapped_column(Integer, default=0)
    valid: Mapped[bool] = mapped_column(Boolean, default=False)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    request_ref: Mapped[str | None] = mapped_column(String(400))
    response_ref: Mapped[str | None] = mapped_column(String(400))
    created_at: Mapped[datetime] = _now()


class Event(Base):
    __tablename__ = "events"
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _now()


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    key: Mapped[str] = mapped_column(String(400))
    sha256: Mapped[str] = mapped_column(String(64), default="")
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    content_type: Mapped[str] = mapped_column(String(100), default="application/octet-stream")
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Upload(Base):
    __tablename__ = "uploads"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(400))
    max_bytes: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GitHubInstallation(Base):
    __tablename__ = "github_installations"
    installation_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="SET NULL"))
    account_login: Mapped[str] = mapped_column(String(200), default="")
    repository_selection: Mapped[str] = mapped_column(String(20), default="selected")
    permissions: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _now()
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (Index("ix_outbox_pending", "status", "next_attempt_at"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(String(32))
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime] = _now()
    created_at: Mapped[datetime] = _now()
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InboundDelivery(Base):
    __tablename__ = "inbound_deliveries"
    source: Mapped[str] = mapped_column(String(20), primary_key=True)
    delivery_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    received_at: Mapped[datetime] = _now()


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    actor: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(400), default="")
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    ip: Mapped[str | None] = mapped_column(INET)
    created_at: Mapped[datetime] = _now()


TENANT_SCOPED_TABLES = ("tasks", "api_keys", "artifacts", "uploads", "audit_log")
