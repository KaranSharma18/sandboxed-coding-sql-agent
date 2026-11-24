"""The runner end to end on the local backend: prepare, run, artifacts, limits, SQL."""

import asyncio
import base64
import json
import shutil

import pytest

from fixloop_core.hashing import sha256_bytes
from fixloop_core.models import ExecPurpose, ExecStatus, OverlayFile, SeedFile
from fixloop_core.objectstore import LocalObjectStore
from fixloop_core.settings import FixloopSettings
from fixloop_core.traces import parse_report
from fixloop_runner.backends.local import LocalBackend
from fixloop_runner.conformance import run_conformance
from fixloop_runner.service import RunnerService
from tests.integration.runner_helpers import (
    CONFIG,
    IS_ROOT,
    put_snapshot,
    python_request,
    runner_settings,
    shared_tmp,
    sql_request,
)

pytestmark = pytest.mark.integration

PROJECT = {
    "calc.py": "def add(a, b):\n    return a - b\n",
    "tests/test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\n"
    "def test_add_zero():\n    assert add(0, 0) == 0\n",
}
FIXED = base64.b64encode(b"def add(a, b):\n    return a + b\n").decode()


@pytest.fixture(scope="module")
def runner_env():
    workdir = shared_tmp("fixloop-runner-")
    objects = shared_tmp("fixloop-objects-")
    settings = FixloopSettings(runner=runner_settings(workdir))
    backend = LocalBackend(settings.runner)
    store = LocalObjectStore(objects)
    yield settings, backend, store
    shutil.rmtree(workdir, ignore_errors=True)
    shutil.rmtree(objects, ignore_errors=True)


def service(runner_env, redis=None):
    settings, backend, store = runner_env
    return RunnerService(settings, CONFIG, backend, store, redis or FakeRedis())


class FakeRedis:
    """Just enough of Redis for `RunnerService.execute` (no stored results, never cancelled)."""

    async def get(self, key):
        return None

    async def exists(self, key):
        return 0


async def test_conformance_probe_runs_and_reports_unsafe(runner_env):
    settings, backend, _ = runner_env
    report = await run_conformance(backend, CONFIG, settings.runner)
    assert report.unsafe and report.isolation == "none"
    assert "no_secrets_in_environment" in report.checks and report.checks["tmp_writable"]["ok"]
    if IS_ROOT:
        assert report.checks["non_root"]["ok"]


async def test_python_prepare_baseline_and_candidate(runner_env):
    _, _, store = runner_env
    svc = service(runner_env)
    snapshot = await put_snapshot(store, "tenant-a", PROJECT)
    prepared = await svc.execute(python_request("task-py", snapshot, purpose=ExecPurpose.prepare))
    assert prepared.env_error is None, prepared.env_error
    assert prepared.env is not None and any(
        line.lower().startswith("pytest") for line in prepared.env.metadata["installed"]
    )

    baseline = await svc.execute(python_request("task-py", snapshot))
    assert baseline.status is ExecStatus.completed and baseline.exit_code == 1 and baseline.cache_hit
    report = parse_report((await store.get_bytes(baseline.artifacts["report.jsonl"].key)).decode())
    assert report.tests["tests/test_calc.py::test_add"].outcome == "failed"
    assert report.tests["tests/test_calc.py::test_add_zero"].outcome == "passed"
    assert "junit.xml" in baseline.artifacts

    candidate = await svc.execute(
        python_request(
            "task-py",
            snapshot,
            purpose=ExecPurpose.candidate,
            attempt=1,
            fingerprint="fix",
            overlay=[
                OverlayFile(path="calc.py", sha256=sha256_bytes(base64.b64decode(FIXED)), content_b64=FIXED)
            ],
            node_ids=["tests/test_calc.py::test_add"],
        )
    )
    assert candidate.exit_code == 0, candidate.stdout_tail
    report = parse_report((await store.get_bytes(candidate.artifacts["report.jsonl"].key)).decode())
    assert list(report.tests) == ["tests/test_calc.py::test_add"]


async def test_protected_file_tampering_is_detected(runner_env):
    _, _, store = runner_env
    files = dict(PROJECT)
    files["tests/conftest.py"] = (
        "import pathlib\n\ndef pytest_sessionfinish(session):\n"
        "    pathlib.Path('tests/test_calc.py').write_text('# rewritten\\n')\n"
    )
    snapshot = await put_snapshot(store, "tenant-a", files)
    protected = {"tests/test_calc.py": sha256_bytes(files["tests/test_calc.py"].encode())}
    result = await service(runner_env).execute(python_request("task-tamper", snapshot, protected=protected))
    assert result.protected_modified == ["tests/test_calc.py"]


async def test_timeout_kills_the_run(runner_env):
    _, _, store = runner_env
    snapshot = await put_snapshot(
        store, "tenant-a", {"tests/test_slow.py": "import time\n\ndef test_slow():\n    time.sleep(60)\n"}
    )
    result = await service(runner_env).execute(python_request("task-slow", snapshot, timeout_s=4))
    assert result.status is ExecStatus.timeout
    assert result.duration_ms < 20_000


async def test_missing_dependency_is_an_env_failure(runner_env):
    _, _, store = runner_env
    snapshot = await put_snapshot(store, "tenant-a", PROJECT)
    result = await service(runner_env).execute(
        python_request("task-env", snapshot, requirements="fixloop-package-that-does-not-exist-xyz==9.9.9\n")
    )
    assert (
        result.env is None and result.env_error and "fixloop-package-that-does-not-exist" in result.env_error
    )


async def test_sql_duckdb_prepare_and_run(runner_env):
    _, _, store = runner_env
    csv = b"id,name,amount\n1,a,10\n2,b,20\n3,c,30\n"
    await store.put_bytes("tenants/tenant-a/seeds/orders.csv", csv)
    seeds = [SeedFile(table="orders", key="tenants/tenant-a/seeds/orders.csv", sha256=sha256_bytes(csv))]
    ddl = "CREATE TABLE orders (id INTEGER PRIMARY KEY, name VARCHAR, amount INTEGER);"
    svc = service(runner_env)
    prepared = await svc.execute(
        sql_request(
            "task-sql", dialect="duckdb", ddl=ddl, seeds=seeds, run_spec=None, purpose=ExecPurpose.prepare
        )
    )
    assert prepared.env_error is None, prepared.env_error
    schema = prepared.env.metadata["schema"]
    assert schema["tables"][0]["name"] == "orders" and schema["tables"][0]["row_count"] == 3
    run_spec = {
        "dialect": "duckdb",
        "candidate_query": "SELECT name, amount FROM orders WHERE amount > 15",
        "datasets": prepared.env.metadata["datasets"],
        "max_rows": 100,
        "statement_timeout_ms": 5000,
        "explain_max_rows": 1000000,
        "assertions": [],
    }
    ran = await svc.execute(
        sql_request(
            "task-sql", dialect="duckdb", ddl=ddl, seeds=seeds, run_spec=run_spec, attempt=1, fingerprint="q1"
        )
    )
    assert ran.status is ExecStatus.completed and ran.cache_hit, ran.stderr_tail
    result = json.loads(await store.get_bytes(ran.artifacts["sql_result.json"].key))
    assert result["ok"] and result["datasets"]["base"]["candidate"]["rows"] == [["b", 20], ["c", 30]]
    assert len(result["datasets"]) == 3  # base + two mutated copies


@pytest.mark.skipif(
    not runner_settings(shared_tmp("probe-")).pg_bin_dir, reason="Postgres binaries not installed"
)
async def test_sql_postgres_prepare_and_run(runner_env):
    _, _, store = runner_env
    csv = b"id,region,amount\n1,eu,10\n2,us,20\n3,eu,5\n"
    await store.put_bytes("tenants/tenant-a/seeds/sales.csv", csv)
    seeds = [SeedFile(table="sales", key="tenants/tenant-a/seeds/sales.csv", sha256=sha256_bytes(csv))]
    ddl = "CREATE TABLE sales (id integer primary key, region text not null, amount integer not null);"
    svc = service(runner_env)
    prepared = await svc.execute(
        sql_request(
            "task-pg",
            dialect="postgres",
            ddl=ddl,
            seeds=seeds,
            run_spec=None,
            purpose=ExecPurpose.prepare,
            mutations=1,
        )
    )
    assert prepared.env_error is None, prepared.env_error
    run_spec = {
        "dialect": "postgres",
        "candidate_query": "SELECT region, sum(amount) AS total FROM sales GROUP BY region ORDER BY region",
        "datasets": ["base"],
        "max_rows": 100,
        "statement_timeout_ms": 5000,
        "explain_max_rows": 1000000,
        "assertions": [],
    }
    ran = await svc.execute(
        sql_request(
            "task-pg",
            dialect="postgres",
            ddl=ddl,
            seeds=seeds,
            run_spec=run_spec,
            attempt=1,
            fingerprint="q",
            mutations=1,
        )
    )
    result = json.loads(await store.get_bytes(ran.artifacts["sql_result.json"].key))
    assert result["ok"], result
    assert result["datasets"]["base"]["candidate"]["rows"] == [["eu", 15], ["us", 20]]


async def test_queue_roundtrip_through_redis(runner_env, redis_queue):
    from fixloop_core.execclient import ExecClient

    _, _, store = runner_env
    svc = service(runner_env, redis_queue)
    snapshot = await put_snapshot(store, "tenant-a", PROJECT)
    stop = asyncio.Event()
    serving = asyncio.create_task(svc.serve(stop))
    try:
        result = await asyncio.wait_for(ExecClient(redis_queue).run(python_request("task-q", snapshot)), 300)
    finally:
        stop.set()
        await asyncio.wait_for(serving, 60)
    assert result.status is ExecStatus.completed and result.exit_code == 1
    assert result.runner_id == "test-runner"
