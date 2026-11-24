"""`LLMService.generate`: the single door through which every model call passes (design section 7).

It owns routing (role, profile, escalation), the L1 cache, budget reservations, shared rate
limits, retries with jitter, circuit breakers and fallbacks, structured-output validation with
format re-asks, finish-reason handling, cost accounting and call records. Nothing else in the
code base talks to a model SDK.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog
from pydantic import ValidationError

from fixloop_core.cachekeys import CALLS_AVOIDED, cache_key
from fixloop_core.config import AppConfig, RoleConfig
from fixloop_core.context import estimate_tokens
from fixloop_core.errors import FormatError, InfraError, ProviderError, ProviderRefusal
from fixloop_core.llm.resilience import Breakers, RateLimiter, backoff_delay
from fixloop_core.llm.schema import provider_schema
from fixloop_core.llm.types import CallContext, LLMResult, ModelBackend, ModelRequest, ModelResponse, T
from fixloop_core.models import Usage
from fixloop_core.observability import LLM_CALLS, LLM_COST, LLM_LATENCY, LLM_TOKENS, LLM_VALIDATION_FAILURES
from fixloop_core.outputs import SCHEMA_VERSION
from fixloop_core.ports import Need
from fixloop_core.prompts import RenderedPrompt

log = structlog.get_logger(__name__)
REFUSALS = {
    "SAFETY",
    "RECITATION",
    "PROHIBITED_CONTENT",
    "BLOCKLIST",
    "SPII",
    "OTHER",
    "LANGUAGE",
    "MODEL_ARMOR",
}
ESCALATABLE = {"repair", "sql_repair"}
__all__ = ["CallContext", "LLMService"]


@dataclass
class _Route:
    role: str
    config: RoleConfig


class LLMService:
    def __init__(
        self,
        *,
        config: AppConfig,
        backend: ModelBackend,
        ledger: Any,
        cache: Any,
        rate_limiter: RateLimiter | None = None,
        breakers: Breakers | None = None,
        provider_wait_s: float = 600.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config = config
        self.backend = backend
        self.ledger = ledger
        self.cache = cache
        self.rate_limiter = rate_limiter or RateLimiter(None, {})
        self.breakers = breakers or Breakers(config.models.breaker)
        self.provider_wait_s = provider_wait_s
        self.sleep = sleep

    # ------------------------------------------------------------------ routing
    def route(self, role: str, ctx: CallContext) -> _Route:
        base = self.config.models.role(role, ctx.profile)
        if ctx.escalated and role in ESCALATABLE:
            escalated = self.config.models.roles["repair_escalated"]
            base = base.model_copy(
                update={
                    "model": escalated.model,
                    "fallback": escalated.fallback,
                    "thinking": escalated.thinking,
                    "timeout_s": escalated.timeout_s,
                    "max_output_tokens": max(
                        base.max_output_tokens,
                        escalated.max_output_tokens if role == "repair" else base.max_output_tokens,
                    ),
                }
            )
        return _Route(role, base)

    # ------------------------------------------------------------------ the call
    async def generate(
        self, role: str, prompt: RenderedPrompt, schema: type[T], ctx: CallContext
    ) -> LLMResult[T]:
        route = self.route(role, ctx)
        json_schema = provider_schema(schema)
        key = cache_key(
            "llm",
            ctx.tenant_id,
            route.config.model,
            prompt.prompt_version,
            SCHEMA_VERSION,
            role,
            route.config.max_output_tokens,
            route.config.thinking,
            prompt.digest(),
        )
        if ctx.use_cache:
            cached = await self.cache.get("llm", key)
            if cached is not None:
                try:
                    value = schema.model_validate(cached["value"])
                except ValidationError:
                    await self.cache.delete(key)
                else:
                    CALLS_AVOIDED.labels("l1").inc()
                    await self._record(
                        ctx,
                        role=role,
                        model=cached.get("model", route.config.model),
                        response=None,
                        cost=0.0,
                        reask=0,
                        valid=True,
                        cache_hit=True,
                        prompt=prompt,
                    )
                    return LLMResult(
                        value=value,
                        usage=Usage(),
                        model=cached.get("model", route.config.model),
                        role=role,
                        cache_hit=True,
                    )

        usage = Usage()
        current = prompt
        max_output = route.config.max_output_tokens
        refusal_fallback_used = False
        fallback_from: str | None = None
        models = [route.config.model] + ([route.config.fallback] if route.config.fallback else [])
        last_problem = ""
        reask = 0
        while True:
            response, model, cost = await self._call(route, current, json_schema, max_output, ctx, models)
            if model != route.config.model:
                fallback_from = route.config.model
            usage = usage.add(
                Usage(
                    model_calls=1,
                    input_tokens=response.prompt_tokens,
                    cached_tokens=response.cached_tokens,
                    output_tokens=response.output_tokens,
                    thinking_tokens=response.thinking_tokens,
                    cost_usd=cost,
                )
            )
            finish = response.finish_reason
            if finish in REFUSALS:
                await self._record(
                    ctx,
                    role=role,
                    model=model,
                    response=response,
                    cost=cost,
                    reask=reask,
                    valid=False,
                    error=f"refusal: {finish}",
                    prompt=current,
                )
                if not refusal_fallback_used and route.config.fallback and model != route.config.fallback:
                    refusal_fallback_used = True
                    models = [route.config.fallback]
                    continue
                raise ProviderRefusal(f"the model refused to answer ({finish})")
            if finish == "MAX_TOKENS":
                last_problem = (
                    "Your output was cut off because it was too long. Produce a smaller change: "
                    "at most 5 edits, each as short as possible."
                )
                valid = False
            else:
                try:
                    value = schema.model_validate_json(response.text or "")
                except ValidationError as exc:
                    last_problem = _validation_message(exc)
                    valid = False
                else:
                    await self._record(
                        ctx,
                        role=role,
                        model=model,
                        response=response,
                        cost=cost,
                        reask=reask,
                        valid=True,
                        prompt=current,
                        fallback_from=fallback_from,
                    )
                    if ctx.use_cache:
                        await self.cache.put(
                            "llm", key, {"value": value.model_dump(mode="json"), "model": model}
                        )
                    return LLMResult(
                        value=value,
                        usage=usage,
                        model=model,
                        role=role,
                        reasks=reask,
                        fallback_from=fallback_from,
                        calls=usage.model_calls,
                    )
            LLM_VALIDATION_FAILURES.labels(role).inc()
            await self._record(
                ctx,
                role=role,
                model=model,
                response=response,
                cost=cost,
                reask=reask,
                valid=valid,
                error=last_problem[:500],
                prompt=current,
                fallback_from=fallback_from,
            )
            if reask >= ctx.format_reasks:
                raise FormatError(
                    f"the model's output failed validation after {reask} re-ask(s): {last_problem[:400]}"
                )
            reask += 1
            current = prompt.with_parts(_reask_part(last_problem, reask))
            if reask >= 2:
                max_output = max(2048, max_output // 2)

    async def _call(
        self,
        route: _Route,
        prompt: RenderedPrompt,
        json_schema: dict[str, Any],
        max_output: int,
        ctx: CallContext,
        models: list[str],
    ) -> tuple[ModelResponse, str, float]:
        retries = self.config.models.retries
        started = time.monotonic()
        estimate = estimate_tokens(prompt.text())
        while True:
            rejected: list[str] = []
            waited_for_transient = False
            for model in models:
                breaker = self.breakers.for_model(model)
                if not breaker.allow():
                    waited_for_transient = True
                    continue
                price = self.config.models.price_for(model)
                for attempt in range(retries.attempts):
                    need = Need(
                        calls=1,
                        input_tokens=estimate,
                        output_tokens=max_output,
                        cost_usd=price.cost(estimate, 0, max_output, 0),
                    )
                    rid = await self.ledger.reserve(
                        ctx.task_id or "-", ctx.tenant_id, need, ctx.task_limits, ctx.tenant_limits
                    )
                    try:
                        await self.rate_limiter.acquire(model, estimate + max_output)
                        response = await self.backend.generate(
                            ModelRequest(
                                role=route.role,
                                model=model,
                                system=prompt.system,
                                parts=prompt.parts,
                                json_schema=json_schema,
                                max_output_tokens=max_output,
                                thinking=route.config.thinking,
                                timeout_s=route.config.timeout_s,
                            )
                        )
                    except ProviderError as exc:
                        await self.ledger.release(ctx.task_id or "-", ctx.tenant_id, rid)
                        breaker.record(False)
                        LLM_CALLS.labels(route.role, model, "error").inc()
                        await self._record(
                            ctx,
                            role=route.role,
                            model=model,
                            response=None,
                            cost=0.0,
                            reask=0,
                            valid=False,
                            error=str(exc)[:500],
                            prompt=prompt,
                        )
                        if not exc.transient:
                            rejected.append(f"{model}: {str(exc)[:200]}")
                            break
                        waited_for_transient = True
                        await self.sleep(
                            backoff_delay(attempt, retries.base_delay_s, retries.max_delay_s, exc.retry_after)
                        )
                        continue
                    except BaseException:
                        await self.ledger.release(ctx.task_id or "-", ctx.tenant_id, rid)
                        raise
                    breaker.record(True)
                    cost = price.cost(
                        response.prompt_tokens,
                        response.cached_tokens,
                        response.output_tokens,
                        response.thinking_tokens,
                    )
                    await self.ledger.settle(
                        ctx.task_id or "-",
                        ctx.tenant_id,
                        rid,
                        Need(
                            calls=1,
                            input_tokens=response.prompt_tokens,
                            output_tokens=response.output_tokens + response.thinking_tokens,
                            cost_usd=cost,
                        ),
                    )
                    LLM_CALLS.labels(route.role, model, "ok").inc()
                    LLM_LATENCY.labels(route.role, model).observe(response.latency_ms / 1000)
                    LLM_COST.labels(model, route.role).inc(cost)
                    for kind, amount in (
                        ("input", response.prompt_tokens),
                        ("cached", response.cached_tokens),
                        ("output", response.output_tokens),
                        ("thinking", response.thinking_tokens),
                    ):
                        LLM_TOKENS.labels(model, kind).inc(amount)
                    return response, model, cost
            if rejected and not waited_for_transient:
                raise InfraError("every model rejected the request: " + "; ".join(rejected), retryable=False)
            if time.monotonic() - started > self.provider_wait_s:
                raise InfraError("model providers are unavailable (primary and fallback)", retryable=True)
            log.warning("models_unavailable_waiting", role=route.role, models=models)
            await self.sleep(min(60.0, self.config.models.breaker.open_seconds))

    async def _record(
        self,
        ctx: CallContext,
        *,
        role: str,
        model: str,
        response: ModelResponse | None,
        cost: float,
        reask: int,
        valid: bool,
        prompt: RenderedPrompt,
        cache_hit: bool = False,
        error: str | None = None,
        fallback_from: str | None = None,
    ) -> None:
        if ctx.recorder is None:
            return
        record = {
            "task_id": ctx.task_id,
            "tenant_id": ctx.tenant_id,
            "attempt_no": ctx.attempt_no,
            "role": role,
            "model": model,
            "fallback_from": fallback_from,
            "prompt_version": prompt.prompt_version,
            "schema_version": SCHEMA_VERSION,
            "input_tokens": response.prompt_tokens if response else 0,
            "cached_tokens": response.cached_tokens if response else 0,
            "output_tokens": response.output_tokens if response else 0,
            "thinking_tokens": response.thinking_tokens if response else 0,
            "cost_usd": cost,
            "latency_ms": response.latency_ms if response else 0,
            "finish_reason": response.finish_reason if response else ("CACHE" if cache_hit else "ERROR"),
            "reask_no": reask,
            "valid": valid,
            "cache_hit": cache_hit,
            "error": error,
            "request_text": prompt.text() if not cache_hit else None,
            "response_text": response.text if response else None,
        }
        try:
            await ctx.recorder(record)
        except Exception as exc:
            log.warning("llm_call_record_failed", error=str(exc))


def _validation_message(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors()[:20]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "(root)"
        lines.append(f"- {location}: {error.get('msg')}")
    return "The JSON did not match the schema:\n" + "\n".join(lines)


def _reask_part(problem: str, reask: int) -> str:
    extra = (
        ""
        if reask < 2
        else ("\nKeep the output short: at most 5 edits, short reasons, and only what the schema requires.")
    )
    return (
        f'<reask n="{reask}">\nYour previous answer could not be used.\n{problem}\n'
        f"Return ONLY a corrected JSON object that matches the schema.{extra}\n</reask>"
    )
