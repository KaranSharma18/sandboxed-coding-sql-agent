"""Helpers for runner tests: a local backend, a world-readable Python with the SQL drivers, requests."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from fixloop_core.config import AppConfig, load_config
from fixloop_core.hashing import sha256_text
from fixloop_core.ids import exec_id
from fixloop_core.models import (
    CommandSpec,
    ExecPurpose,
    ExecRequest,
    PrepareSpec,
    PythonPrepare,
    SeedFile,
    SnapshotRef,
    SqlPrepare,
)
from fixloop_core.objectstore import LocalObjectStore
from fixloop_core.settings import RunnerSettings
from fixloop_core.snapshot import build_snapshot

ROOT = Path(__file__).resolve().parents[2]
CONFIG: AppConfig = load_config(ROOT / "config")
IS_ROOT = os.name != "nt" and os.geteuid() == 0
INDEX_URL = os.environ.get("FIXLOOP_TEST_INDEX_URL", "https://pypi.org/simple")


def shared_tmp(prefix: str) -> Path:
    """A temp dir every user can enter (the local backend drops to an unprivileged user as root)."""
    path = Path(tempfile.mkdtemp(prefix=prefix))
    os.chmod(path, 0o755)
    return path


def base_python() -> str:
    if not IS_ROOT:
        return sys.executable
    for candidate in ("/usr/bin/python3", "/usr/local/bin/python3"):
        if os.path.exists(candidate):
            return candidate
    raise RuntimeError("no world-readable python found")


_SQL_PYTHON: str | None = None


def sql_python() -> str:
    """An interpreter with duckdb and psycopg that the sandbox user can run."""
    global _SQL_PYTHON
    if _SQL_PYTHON:
        return _SQL_PYTHON
    if not IS_ROOT:
        _SQL_PYTHON = sys.executable
        return _SQL_PYTHON
    venv = Path("/tmp/fixloop-test-sqlvenv")
    python = venv / "bin" / "python"
    if not python.exists():
        uv = shutil.which("uv") or "uv"
        subprocess.run(
            [uv, "venv", str(venv), "--python", base_python(), "--no-project"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [uv, "pip", "install", "--python", str(python), "duckdb>=1.1", "psycopg[binary]>=3.2"],
            check=True,
            capture_output=True,
        )
        subprocess.run(["chmod", "-R", "a+rX", str(venv)], check=True)
    _SQL_PYTHON = str(python)
    return _SQL_PYTHON


def runner_settings(workdir: Path, **overrides: object) -> RunnerSettings:
    values: dict[str, object] = {
        "backend": "local",
        "allow_unsafe_local": True,
        "workdir": workdir,
        "index_url": INDEX_URL,
        "local_pythons": {"3.12": base_python(), "default": base_python(), "sql": sql_python()},
        "pg_bin_dir": next(
            (p for p in ("/usr/lib/postgresql/17/bin", "/usr/lib/postgresql/16/bin") if os.path.exists(p)),
            None,
        ),
        "runner_id": "test-runner",
    }
    values.update(overrides)
    return RunnerSettings(**values)


async def put_snapshot(store: LocalObjectStore, tenant: str, files: dict[str, str]) -> SnapshotRef:
    build = build_snapshot({path: content.encode() for path, content in files.items()})
    key = f"tenants/{tenant}/snapshots/{build.sha256}.tar.zst"
    await store.put_bytes(key, build.tar_zst)
    return SnapshotRef(key=key, sha256=build.sha256, size=len(build.tar_zst), files=len(build.files))


def python_request(
    task_id: str,
    snapshot: SnapshotRef,
    *,
    requirements: str = "",
    purpose=ExecPurpose.baseline,
    node_ids: list[str] | None = None,
    attempt: int = 0,
    fingerprint: str = "",
    overlay=None,
    timeout_s: int = 120,
    protected: dict[str, str] | None = None,
    tenant: str = "tenant-a",
) -> ExecRequest:
    env_hash = sha256_text("py3.12|" + requirements + "|" + INDEX_URL)
    return ExecRequest(
        exec_id=exec_id(task_id, attempt, purpose.value, fingerprint),
        tenant_id=tenant,
        task_id=task_id,
        purpose=purpose,
        prepare=PrepareSpec(
            family="python",
            image="python-3.12",
            env_hash=env_hash,
            python=PythonPrepare(python_version="3.12", requirements=requirements),
        ),
        snapshot=snapshot,
        overlay=overlay or [],
        command=CommandSpec(
            template="python-prepare" if purpose is ExecPurpose.prepare else "pytest-default",
            args={} if purpose is ExecPurpose.prepare else {"node_ids": node_ids or [], "test_timeout": 20},
        ),
        limits=CONFIG.limits.sandbox.limits(timeout_s),
        protected_hashes=protected or {},
        deadline_ts=time.time() + 900,
    )


def sql_request(
    task_id: str,
    *,
    dialect: str,
    ddl: str,
    seeds: list[SeedFile],
    run_spec: dict | None,
    purpose=ExecPurpose.sql,
    attempt: int = 0,
    fingerprint: str = "",
    mutations: int = 2,
    tenant: str = "tenant-a",
) -> ExecRequest:
    from fixloop_core.models import OverlayFile

    env_hash = sha256_text(f"{dialect}|{ddl}|{[s.sha256 for s in seeds]}|{mutations}")
    extra = []
    if run_spec is not None:
        import base64
        import json

        extra = [
            OverlayFile(
                path=".fixloop/sql_run.json",
                content_b64=base64.b64encode(json.dumps(run_spec).encode()).decode(),
            )
        ]
    return ExecRequest(
        exec_id=exec_id(task_id, attempt, purpose.value, fingerprint),
        tenant_id=tenant,
        task_id=task_id,
        purpose=purpose,
        prepare=PrepareSpec(
            family="sql",
            image=f"sql-{dialect}",
            env_hash=env_hash,
            sql=SqlPrepare(dialect=dialect, ddl=ddl, seeds=seeds, mutations=mutations, mutation_seed=7),
        ),
        extra_files=extra,
        command=CommandSpec(template="sql-prepare" if purpose is ExecPurpose.prepare else "sql-run"),
        limits=CONFIG.limits.sandbox.limits(60),
        deadline_ts=time.time() + 600,
    )
