from pathlib import Path
from types import SimpleNamespace

import pytest

from fixloop_core.budget import MemoryBudgetLedger
from fixloop_core.cache import MemoryCache
from fixloop_core.config import TenantLimits, load_config
from fixloop_core.errors import BudgetExhausted, FormatError, ProviderRefusal
from fixloop_core.llm.gemini import GeminiBackend
from fixloop_core.llm.resilience import Breakers
from fixloop_core.llm.schema import check_supported, provider_schema
from fixloop_core.llm.scripted import ScriptedBackend, ScriptStep
from fixloop_core.llm.service import CallContext, LLMService
from fixloop_core.llm.types import ModelRequest
from fixloop_core.models import BudgetLimits
from fixloop_core.outputs import ROLE_SCHEMAS, Plan
from fixloop_core.prompts import RenderedPrompt
from fixloop_core.settings import GeminiSettings

CONFIG = load_config(Path(__file__).resolve().parents[2] / "config")
TENANT = TenantLimits(
    running_tasks=4,
    queued_tasks=100,
    daily_cost_usd=25,
    tasks_per_day=500,
    api_requests_per_minute=60,
    sandbox_seconds_per_day=10000,
)
PLAN = {
    "hypothesis": "off by one",
    "approach": "fix the loop",
    "files_to_change": ["pkg/a.py"],
    "files_to_read": [],
    "tests_expected_to_pass": ["tests/test_a.py::test_x"],
    "risk": "low",
    "dependency_requests": [],
}
PROMPT = RenderedPrompt(role="plan", system="rules", parts=("task", "files"))


async def no_sleep(_: float) -> None:
    return None


def service(steps, limits=None, cache=None):
    backend = ScriptedBackend(steps=steps)
    calls: list[dict] = []

    async def recorder(record):
        calls.append(record)

    svc = LLMService(
        config=CONFIG,
        backend=backend,
        ledger=MemoryBudgetLedger(),
        cache=cache or MemoryCache(),
        breakers=Breakers(CONFIG.models.breaker),
        sleep=no_sleep,
        provider_wait_s=0,
    )
    ctx = CallContext(
        tenant_id="t1",
        task_id="task-1",
        task_limits=limits or BudgetLimits(),
        tenant_limits=TENANT,
        recorder=recorder,
    )
    return svc, ctx, backend, calls


@pytest.mark.parametrize("role", sorted(ROLE_SCHEMAS))
def test_every_role_schema_is_provider_safe(role):
    schema = provider_schema(ROLE_SCHEMAS[role])
    assert "$ref" not in str(schema) and check_supported(schema) == []


async def test_happy_path_records_cost():
    svc, ctx, backend, calls = service([ScriptStep(role="plan", output=PLAN)])
    result = await svc.generate("plan", PROMPT, Plan, ctx)
    assert result.value.risk == "low" and result.usage.model_calls == 1 and result.usage.cost_usd > 0
    assert (
        backend.requests[0].model == "gemini-3.8-flash"
        and backend.requests[0].json_schema["type"] == "object"
    )
    assert calls[-1]["valid"] is True


async def test_format_reask_then_success():
    svc, ctx, backend, _ = service(
        [ScriptStep(role="plan", raw="not json"), ScriptStep(role="plan", output=PLAN)]
    )
    result = await svc.generate("plan", PROMPT, Plan, ctx)
    assert result.reasks == 1 and result.usage.model_calls == 2
    assert "<reask" in backend.requests[1].parts[-1]


async def test_format_error_after_two_reasks():
    svc, ctx, _, _ = service([ScriptStep(role="plan", raw="{}", times=0)])
    with pytest.raises(FormatError):
        await svc.generate("plan", PROMPT, Plan, ctx)


async def test_max_tokens_asks_for_smaller_change():
    svc, ctx, backend, _ = service(
        [ScriptStep(role="plan", output=PLAN, error="max_tokens"), ScriptStep(role="plan", output=PLAN)]
    )
    await svc.generate("plan", PROMPT, Plan, ctx)
    assert "cut off" in backend.requests[1].parts[-1]


async def test_refusal_goes_to_fallback_then_gives_up():
    svc, ctx, backend, _ = service(
        [ScriptStep(role="plan", output=PLAN, error="refusal"), ScriptStep(role="plan", output=PLAN)]
    )
    result = await svc.generate("plan", PROMPT, Plan, ctx)
    assert backend.requests[1].model == "gemini-3.6-flash" and result.model == "gemini-3.6-flash"
    svc, ctx, _, _ = service([ScriptStep(role="plan", output=PLAN, error="refusal", times=0)])
    with pytest.raises(ProviderRefusal):
        await svc.generate("plan", PROMPT, Plan, ctx)


async def test_transient_errors_retry_and_breaker_opens_to_fallback():
    steps = [ScriptStep(role="plan", error="500", times=5), ScriptStep(role="plan", output=PLAN)]
    svc, ctx, backend, _ = service(steps)
    result = await svc.generate("plan", PROMPT, Plan, ctx)
    assert result.value.hypothesis == "off by one"
    assert {r.model for r in backend.requests} >= {"gemini-3.8-flash"}


async def test_budget_is_enforced_before_spending():
    svc, ctx, _, _ = service(
        [ScriptStep(role="plan", raw="nope"), ScriptStep(role="plan", output=PLAN)],
        limits=BudgetLimits(max_model_calls=1),
    )
    with pytest.raises(BudgetExhausted) as info:
        await svc.generate("plan", PROMPT, Plan, ctx)
    assert info.value.dimension == "model_calls"


async def test_l1_cache_hit_costs_nothing():
    cache = MemoryCache()
    svc, ctx, backend, calls = service([ScriptStep(role="plan", output=PLAN)], cache=cache)
    await svc.generate("plan", PROMPT, Plan, ctx)
    again = await svc.generate("plan", PROMPT, Plan, ctx)
    assert again.cache_hit and again.usage.model_calls == 0 and len(backend.requests) == 1
    assert calls[-1]["cache_hit"] is True


def test_escalation_routes_repairs_to_pro():
    svc, ctx, _, _ = service([])
    ctx.escalated = True
    assert svc.route("repair", ctx).config.model == "gemini-3.1-pro-preview"
    assert svc.route("plan", ctx).config.model == "gemini-3.8-flash"
    ctx.escalated, ctx.profile = False, "quality"
    assert svc.route("plan", ctx).config.model == "gemini-3.1-pro-preview"


class FakeModels:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def fake_client(models):
    return SimpleNamespace(aio=SimpleNamespace(models=models))


async def test_gemini_backend_maps_response_and_config():
    part = SimpleNamespace(text='{"a": 1}', thought=False)
    response = SimpleNamespace(
        candidates=[
            SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"), content=SimpleNamespace(parts=[part]))
        ],
        usage_metadata=SimpleNamespace(
            prompt_token_count=1000,
            cached_content_token_count=600,
            candidates_token_count=50,
            thoughts_token_count=20,
        ),
        prompt_feedback=None,
    )
    models = FakeModels(response=response)
    backend = GeminiBackend(GeminiSettings(api_key="x"), client=fake_client(models))
    request = ModelRequest(
        role="plan",
        model="gemini-3.8-flash",
        system="s",
        parts=("a", "b"),
        json_schema={"type": "object"},
        max_output_tokens=100,
        thinking="medium",
        timeout_s=30,
    )
    result = await backend.generate(request)
    assert (result.text, result.prompt_tokens, result.cached_tokens, result.thinking_tokens) == (
        '{"a": 1}',
        1000,
        600,
        20,
    )
    config = models.calls[0]["config"]
    assert (
        config.response_json_schema == {"type": "object"} and config.response_mime_type == "application/json"
    )
    assert config.thinking_config.thinking_level.name == "MEDIUM"
    assert len(models.calls[0]["contents"][0].parts) == 2


async def test_gemini_backend_maps_errors():
    from google.genai import errors

    from fixloop_core.errors import ProviderError

    backend = GeminiBackend(
        GeminiSettings(api_key="x"),
        client=fake_client(FakeModels(error=errors.ServerError(503, {"error": {"message": "busy"}}))),
    )
    request = ModelRequest(
        role="plan",
        model="m",
        system="s",
        parts=("a",),
        json_schema={},
        max_output_tokens=10,
        thinking="low",
        timeout_s=5,
    )
    with pytest.raises(ProviderError) as info:
        await backend.generate(request)
    assert info.value.transient and info.value.status == 503
