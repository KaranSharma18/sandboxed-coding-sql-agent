"""Isolation self-test (ADR-13): prove the sandbox is what we think it is, continuously.

A probe runs inside a sandbox exactly like candidate code would and reports what it can see and
do. The runner checks the report against what must be true. A runner whose sandbox fails a
required check stops taking work (fail closed) and the failure is a metric and a log line. The
test runs at start-up and every `conformance_interval_s`, because isolation can silently change
(a daemon upgrade, a changed default, a node with a different runtime).
"""

from __future__ import annotations

import json
import time
from typing import Any

from fixloop_core.config import AppConfig
from fixloop_core.models import ExecStatus
from fixloop_core.observability import CONFORMANCE_FAILURES
from fixloop_core.settings import RunnerSettings
from fixloop_runner.backends.base import ConformanceReport, RunSpec, SandboxBackend
from fixloop_runner.workspace import Workspace

PROBE = r"""
import json, os, platform, socket, sys

checks = {}

def check(name, required=True):
    def wrap(fn):
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        checks[name] = {"ok": bool(ok), "detail": str(detail)[:300], "required": required}
        return fn
    return wrap

def status_field(name):
    with open("/proc/self/status") as handle:
        for line in handle:
            if line.startswith(name + ":"):
                return line.split(":", 1)[1].strip()
    return None

@check("non_root")
def _():
    return os.getuid() != 0, f"uid={os.getuid()}"

@check("no_network")
def _():
    for host, port in (("1.1.1.1", 53), ("8.8.8.8", 443)):
        try:
            socket.create_connection((host, port), timeout=2).close()
            return False, f"connected to {host}:{port}"
        except OSError:
            pass
    try:
        socket.getaddrinfo("pypi.org", 443)
        return False, "DNS resolution works"
    except OSError:
        return True, "no route out"

@check("no_capabilities")
def _():
    value = status_field("CapEff")
    if value is None:
        return True, "not reported"
    return int(value, 16) == 0, f"CapEff={value}"

@check("no_new_privileges")
def _():
    value = status_field("NoNewPrivs")
    if value is None:
        return True, "not reported"
    return value == "1", f"NoNewPrivs={value}"

@check("read_only_root")
def _():
    for target in ("/usr/fixloop-probe", "/etc/fixloop-probe"):
        try:
            with open(target, "w") as handle:
                handle.write("x")
            os.remove(target)
            return False, f"wrote {target}"
        except OSError:
            pass
    return True, "root filesystem is read-only"

@check("tmp_writable")
def _():
    path = os.path.join(os.environ.get("TMPDIR", "/tmp"), "fixloop-probe")
    with open(path, "w") as handle:
        handle.write("x")
    os.remove(path)
    return True, "ok"

@check("workspace_writable")
def _():
    with open(".fixloop/probe-write", "w") as handle:
        handle.write("x")
    return True, "ok"

@check("no_docker_socket")
def _():
    present = [p for p in ("/var/run/docker.sock", "/run/docker.sock", "/run/containerd/containerd.sock")
               if os.path.exists(p)]
    return not present, ",".join(present) or "absent"

@check("no_secrets_in_environment")
def _():
    markers = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AWS_", "GEMINI", "GOOGLE_",
               "FIXLOOP_DATABASE", "FIXLOOP_REDIS", "FIXLOOP_GITHUB")
    found = [k for k in os.environ if any(m in k.upper() for m in markers)]
    return not found, ",".join(found) or "clean"

@check("memory_limited", required=False)
def _():
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        if os.path.exists(path):
            value = open(path).read().strip()
            return value != "max" and int(value) < 1 << 50, value
    return False, "no cgroup memory file"

@check("pids_limited", required=False)
def _():
    for path in ("/sys/fs/cgroup/pids.max", "/sys/fs/cgroup/pids/pids.max"):
        if os.path.exists(path):
            value = open(path).read().strip()
            return value != "max", value
    return False, "no cgroup pids file"

def isolation():
    try:
        if os.path.exists("/proc/self/status"):
            with open("/proc/version") as handle:
                version = handle.read()
            if platform.release() == "4.4.0" and "2016" in version:
                return "gvisor"
    except OSError:
        pass
    return "runc" if os.path.exists("/.dockerenv") else "unknown"

out = sys.argv[1]
os.makedirs(out, exist_ok=True)
with open(os.path.join(out, "conformance.json"), "w") as handle:
    json.dump({"checks": checks, "isolation": isolation(), "python": sys.version.split()[0]}, handle)
"""


async def run_conformance(
    backend: SandboxBackend, config: AppConfig, settings: RunnerSettings, image: str = "python-3.12"
) -> ConformanceReport:
    exec_id = f"conf-{settings.runner_id[:20]}-{int(time.time() * 1000)}".replace("_", "-").lower()
    placeholders = backend.placeholders("python", None, image, exec_id)
    spec = RunSpec(
        exec_id=exec_id,
        tenant_id="system",
        family="python",
        image=image,
        image_ref=config.image_ref(image, settings.image_overrides),
        argv=[
            placeholders["SYSPYTHON"],
            f"{placeholders['WORKSPACE']}/.fixloop/probe.py",
            placeholders["OUT"],
        ],
        workspace=Workspace(files={".fixloop/probe.py": PROBE.encode()}),
        env_name=None,
        env_writable=False,
        network="none",
        limits=config.limits.sandbox.limits(60),
        purpose="conformance",
    )
    output = await backend.run(spec)
    unsafe = backend.name == "local"
    if output.status is not ExecStatus.completed or "conformance.json" not in output.files:
        detail = output.error or output.stderr.decode(errors="replace")[-500:]
        report = ConformanceReport(
            backend.name,
            "unknown",
            False,
            {"probe_ran": {"ok": False, "detail": detail, "required": True}},
            unsafe=unsafe,
        )
    else:
        data: dict[str, Any] = json.loads(output.files["conformance.json"])
        checks = data.get("checks", {})
        isolation = "none" if unsafe else data.get("isolation", "unknown")
        if settings.isolation == "production":
            checks["gvisor_runtime"] = {"ok": isolation == "gvisor", "detail": isolation, "required": True}
        report = ConformanceReport(backend.name, isolation, False, checks, unsafe=unsafe)
        report.passed = not report.failures()
    for name in report.failures():
        CONFORMANCE_FAILURES.labels(name).inc()
    return report
