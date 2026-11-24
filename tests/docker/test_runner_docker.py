"""The runner on the real Docker backend.

Needs a Docker daemon and the sandbox images (`uv run poe images`). Run with `uv run poe test-docker`.
Overrides: FIXLOOP_TEST_PYTHON_IMAGE (image for python-3.12), FIXLOOP_TEST_PROXY_NETWORK.
"""

import base64
import os
import shutil

import pytest

from fixloop_core.hashing import sha256_bytes
from fixloop_core.models import ExecPurpose, ExecStatus, OverlayFile
from fixloop_core.objectstore import LocalObjectStore
from fixloop_core.settings import FixloopSettings, RunnerSettings
from fixloop_core.traces import parse_report
from fixloop_runner.conformance import run_conformance
from fixloop_runner.service import RunnerService
from tests.integration.runner_helpers import CONFIG, put_snapshot, python_request, shared_tmp

pytestmark = pytest.mark.docker
IMAGE = os.environ.get("FIXLOOP_TEST_PYTHON_IMAGE", CONFIG.templates.images["python-3.12"].ref)
NETWORK = os.environ.get("FIXLOOP_TEST_PROXY_NETWORK", "fixloop-sandbox-proxy")
PROJECT = {
    "calc.py": "def add(a, b):\n    return a - b\n",
    "tests/test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
}


def _docker_ready() -> str | None:
    try:
        import docker

        client = docker.from_env(timeout=10)
        client.images.get(IMAGE)
        client.networks.get(NETWORK)
        return None
    except Exception as exc:
        return f"docker backend test needs a daemon, image {IMAGE} and network {NETWORK}: {exc}"


@pytest.fixture(scope="module")
def docker_env():
    reason = _docker_ready()
    if reason:
        pytest.skip(reason)
    from fixloop_runner.backends.docker import DockerBackend

    workdir, objects = shared_tmp("fixloop-docker-"), shared_tmp("fixloop-docker-objects-")
    settings = FixloopSettings(
        runner=RunnerSettings(
            backend="docker",
            workdir=workdir,
            runner_id="docker-test",
            proxy_network=NETWORK,
            index_url="https://pypi.org/simple",
            image_overrides={"python-3.12": IMAGE},
        )
    )
    backend = DockerBackend(settings.runner)
    store = LocalObjectStore(objects)
    yield settings, backend, store
    shutil.rmtree(workdir, ignore_errors=True)
    shutil.rmtree(objects, ignore_errors=True)


class NoRedis:
    async def get(self, key):
        return None

    async def exists(self, key):
        return 0


def svc(docker_env):
    settings, backend, store = docker_env
    return RunnerService(settings, CONFIG, backend, store, NoRedis())


async def test_conformance_passes(docker_env):
    settings, backend, _ = docker_env
    report = await run_conformance(backend, CONFIG, settings.runner)
    assert report.passed, report.checks
    assert report.checks["no_network"]["ok"] and report.checks["read_only_root"]["ok"]
    assert report.checks["no_capabilities"]["ok"] and report.checks["non_root"]["ok"]


async def test_prepare_baseline_candidate(docker_env):
    _, _, store = docker_env
    service = svc(docker_env)
    snapshot = await put_snapshot(store, "tenant-d", PROJECT)
    prepared = await service.execute(
        python_request("dtask", snapshot, purpose=ExecPurpose.prepare, tenant="tenant-d")
    )
    assert prepared.env_error is None, prepared.env_error
    baseline = await service.execute(python_request("dtask", snapshot, tenant="tenant-d"))
    assert baseline.status is ExecStatus.completed and baseline.exit_code == 1, baseline.stderr_tail
    report = parse_report((await store.get_bytes(baseline.artifacts["report.jsonl"].key)).decode())
    assert report.tests["tests/test_calc.py::test_add"].outcome == "failed"
    fixed = b"def add(a, b):\n    return a + b\n"
    candidate = await service.execute(
        python_request(
            "dtask",
            snapshot,
            purpose=ExecPurpose.candidate,
            attempt=1,
            fingerprint="f",
            tenant="tenant-d",
            overlay=[
                OverlayFile(
                    path="calc.py", sha256=sha256_bytes(fixed), content_b64=base64.b64encode(fixed).decode()
                )
            ],
        )
    )
    assert candidate.exit_code == 0 and candidate.cache_hit


async def test_timeout_and_memory_limits(docker_env):
    _, _, store = docker_env
    service = svc(docker_env)
    snapshot = await put_snapshot(
        store,
        "tenant-d",
        {
            "tests/test_slow.py": "import time\n\ndef test_slow():\n    time.sleep(60)\n",
            "tests/test_big.py": "def test_big():\n    blocks = [bytearray(64 * 1024 * 1024) for _ in range(40)]\n"
            "    assert blocks\n",
        },
    )
    slow = await service.execute(
        python_request("dlimits", snapshot, timeout_s=5, tenant="tenant-d", node_ids=["tests/test_slow.py"])
    )
    assert slow.status is ExecStatus.timeout
    request = python_request(
        "dlimits", snapshot, attempt=1, fingerprint="big", tenant="tenant-d", node_ids=["tests/test_big.py"]
    )
    request.limits.memory_mb = 256
    big = await service.execute(request)
    assert big.status in (ExecStatus.oom, ExecStatus.killed) or big.exit_code != 0


async def test_reap_leaves_nothing_behind(docker_env):
    import docker

    _, backend, _ = docker_env
    await backend.reap()
    client = docker.from_env()
    leftovers = client.containers.list(
        all=True, filters={"label": ["fixloop.runner=docker-test", "fixloop.role=sandbox"]}
    )
    assert leftovers == []
    volumes = client.volumes.list(
        filters={"label": ["fixloop.runner=docker-test", "fixloop.role=sandbox-volume"]}
    )
    assert volumes == []
