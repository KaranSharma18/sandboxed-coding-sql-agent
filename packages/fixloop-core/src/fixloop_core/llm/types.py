"""Request/response types between the LLM service and model backends."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel

from fixloop_core.config import TenantLimits
from fixloop_core.models import BudgetLimits, Usage

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class ModelRequest:
    role: str
    model: str
    system: str
    parts: tuple[str, ...]
    json_schema: dict[str, Any]
    max_output_tokens: int
    thinking: str
    timeout_s: float
    metadata: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)


@dataclass
class ModelResponse:
    text: str
    finish_reason: str = "STOP"
    prompt_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    latency_ms: int = 0
    model: str = ""


class ModelBackend(Protocol):
    name: str

    async def generate(self, request: ModelRequest) -> ModelResponse: ...


@dataclass
class LLMResult(Generic[T]):
    value: T
    usage: Usage
    model: str
    role: str
    cache_hit: bool = False
    reasks: int = 0
    fallback_from: str | None = None
    calls: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class CallContext:
    tenant_id: str
    task_id: str | None
    task_limits: BudgetLimits
    tenant_limits: TenantLimits
    attempt_no: int | None = None
    profile: str = "default"
    escalated: bool = False
    use_cache: bool = True
    format_reasks: int = 2
    recorder: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    warnings: list[str] = field(default_factory=list)
