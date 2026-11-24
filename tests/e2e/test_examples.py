"""The shipped examples work end to end with the shipped demo script (no Gemini key needed)."""

from pathlib import Path

import pytest

from fixloop_core.llm.scripted import ScriptedBackend
from fixloop_core.spec import TaskSpec
from tests.e2e.test_loop import submit, wait_done

pytestmark = pytest.mark.e2e
EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def files_of(folder: Path) -> dict[str, str]:
    return {
        p.relative_to(folder).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(folder.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


async def test_buggy_dates_example(stack):
    backend = ScriptedBackend.from_yaml(EXAMPLES / "demo-script.yaml")
    stack.resources.deps.llm.backend = backend
    spec = TaskSpec.model_validate(
        {
            "kind": "python.fix",
            "description": "parse_dates returns wrong dates",
            "source": {"type": "inline", "files": files_of(EXAMPLES / "buggy-dates")},
            "oracle": {"type": "pytest", "target_tests": ["tests/test_dates.py::test_parse_dates"]},
        }
    )
    task, _attempts = await wait_done(stack, await submit(stack, spec, "example-dates"))
    assert task.outcome == "verified", task.result
    assert task.result["verification"]["level"] == "no_regressions"


async def test_sql_example(stack):
    stack.resources.deps.llm.backend = ScriptedBackend.from_yaml(EXAMPLES / "demo-script.yaml")
    folder = EXAMPLES / "sql-duckdb"
    spec = TaskSpec.model_validate(
        {
            "kind": "sql.write",
            "description": "Revenue from paid orders and the number of paying customers per country, highest revenue first.",
            "source": {
                "type": "sql_snapshot",
                "ddl": (folder / "schema.sql").read_text(),
                "seeds": [
                    {"table": "customers", "inline_csv": (folder / "customers.csv").read_text()},
                    {"table": "orders", "inline_csv": (folder / "orders.csv").read_text()},
                ],
            },
            "oracle": {
                "type": "sql",
                "dialect": "duckdb",
                "reference_query": (folder / "reference.sql").read_text(),
                "order_matters": True,
                "sort_key": ["revenue"],
            },
        }
    )
    task, _ = await wait_done(stack, await submit(stack, spec, "example-sql"))
    assert task.outcome == "verified", task.result
    assert task.result["verification"]["level"] == "differential_passed"
