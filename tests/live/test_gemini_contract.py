"""Live contract tests: every role returns schema-valid output from the real Gemini API.

Run with `uv run pytest -m live` and FIXLOOP_GEMINI__API_KEY set. Cost: a few cents per run.
"""

import os
from pathlib import Path

import pytest

from fixloop_core.budget import MemoryBudgetLedger
from fixloop_core.cache import MemoryCache
from fixloop_core.config import TenantLimits, load_config
from fixloop_core.llm.gemini import GeminiBackend
from fixloop_core.llm.service import CallContext, LLMService
from fixloop_core.models import BudgetLimits
from fixloop_core.outputs import Patch, Plan, SqlCandidate, TraceDigest
from fixloop_core.prompts import RenderedPrompt
from fixloop_core.settings import GeminiSettings

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("FIXLOOP_GEMINI__API_KEY"), reason="needs FIXLOOP_GEMINI__API_KEY"),
]
CONFIG = load_config(Path(__file__).resolve().parents[2] / "config")
CODE = 'FILE calc.py\n<untrusted kind="file">\ndef add(a, b):\n    return a - b\n</untrusted>'
TRACE = "tests/test_calc.py::test_add FAILED\nassert add(2, 3) == 5\nE   assert -1 == 5"


@pytest.mark.parametrize(
    ("role", "schema", "instruction"),
    [
        ("plan", Plan, "Plan the fix."),
        ("patch", Patch, "Write the patch: replace `return a - b` with `return a + b` in calc.py."),
        ("digest", TraceDigest, "Summarize the failure."),
        ("sql_candidate", SqlCandidate, "Write a DuckDB query returning 1 AS one."),
    ],
)
async def test_role_contract(role, schema, instruction):
    service = LLMService(
        config=CONFIG,
        backend=GeminiBackend(GeminiSettings(api_key=os.environ["FIXLOOP_GEMINI__API_KEY"])),
        ledger=MemoryBudgetLedger(),
        cache=MemoryCache(),
    )
    ctx = CallContext(
        tenant_id="live",
        task_id="live",
        task_limits=BudgetLimits(max_cost_usd=0.2),
        tenant_limits=TenantLimits(
            running_tasks=1,
            queued_tasks=1,
            daily_cost_usd=1,
            tasks_per_day=10,
            api_requests_per_minute=10,
            sandbox_seconds_per_day=10,
        ),
        use_cache=False,
    )
    prompt = RenderedPrompt(
        role, "You are a careful software engineer. Answer in JSON only.", (CODE, TRACE, instruction)
    )
    result = await service.generate(role, prompt, schema, ctx)
    assert isinstance(result.value, schema)
    assert result.usage.cost_usd > 0
