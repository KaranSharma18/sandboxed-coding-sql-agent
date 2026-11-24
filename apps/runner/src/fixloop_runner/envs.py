"""Preparing and caching execution environments (design section 9, ADR-15).

An environment is everything a run needs that does not change between candidates: the Python
virtual environment with the project's dependencies, or the SQL database built from the DDL and
seed data (plus the mutated copies used for differential testing). It is built once per
(tenant, env hash) on a runner, with network access only to the package proxy, and every later
run mounts it read-only. Environments are per tenant, so one tenant's preparation can never
leave anything behind for another.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from fixloop_core.config import AppConfig
from fixloop_core.errors import InfraError
from fixloop_core.hashing import sha256_bytes, sha256_text
from fixloop_core.models import EnvRef, ExecRequest, ExecStatus
from fixloop_core.objectstore import ObjectStore
from fixloop_runner.backends.base import RunOutput, RunSpec, SandboxBackend
from fixloop_runner.templates import build_argv
from fixloop_runner.workspace import Workspace


def env_volume_name(tenant_id: str, env_hash: str) -> str:
    return f"fixloop-env-{sha256_text(str(tenant_id))[:12]}-{env_hash[:24]}"


@dataclass
class EnvOutcome:
    name: str
    env: EnvRef | None
    hit: bool
    output: RunOutput | None = None
    error: str | None = None
    seconds: float = 0.0
    report: dict[str, Any] = field(default_factory=dict)


class EnvManager:
    def __init__(self, backend: SandboxBackend, store: ObjectStore, config: AppConfig) -> None:
        self.backend = backend
        self.store = store
        self.config = config
        self._locks: dict[str, asyncio.Lock] = {}

    async def ensure(self, request: ExecRequest, image_ref: str, cancel_check: Any = None) -> EnvOutcome:
        prepare = request.prepare
        name = env_volume_name(request.tenant_id, prepare.env_hash)
        lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock:  # two runs needing the same new environment prepare it once
            metadata = await self.backend.env_metadata(name)
            if metadata is not None:
                return EnvOutcome(
                    name,
                    EnvRef(
                        env_hash=prepare.env_hash,
                        image=prepare.image,
                        family=prepare.family,
                        metadata=metadata,
                    ),
                    hit=True,
                )
            return await self._prepare(request, name, image_ref, cancel_check)

    async def _prepare(
        self, request: ExecRequest, name: str, image_ref: str, cancel_check: Any
    ) -> EnvOutcome:
        prepare = request.prepare
        template_id = "python-prepare" if prepare.family == "python" else "sql-prepare"
        template = self.config.templates.templates[template_id]
        files = await self._inputs(request)
        exec_id = f"{request.exec_id}-prep"
        argv = build_argv(
            template, {}, self.backend.placeholders(prepare.family, name, prepare.image, exec_id)
        )
        sandbox = self.config.limits.sandbox
        limits = request.limits.model_copy(
            update={"timeout_s": sandbox.prepare_timeout_s, "fsize_mb": max(request.limits.fsize_mb, 2048)}
        )
        deadline = time.time() + sandbox.prepare_timeout_s
        spec = RunSpec(
            exec_id=exec_id,
            tenant_id=request.tenant_id,
            family=prepare.family,
            image=prepare.image,
            image_ref=image_ref,
            argv=argv,
            workspace=Workspace(files=files),
            env_name=name,
            env_writable=True,
            network=template.network,
            limits=limits,
            deadline_ts=min(deadline, request.deadline_ts) if request.deadline_ts else deadline,
            cancel_check=cancel_check,
            purpose="prepare",
            task_id=request.task_id,
        )
        output = await self.backend.run(spec)
        report_name = "prepare.json" if prepare.family == "python" else "env.json"
        report: dict[str, Any] = {}
        if report_name in output.files:
            try:
                report = json.loads(output.files[report_name])
            except ValueError:
                report = {}
        if output.status is ExecStatus.completed and output.exit_code == 0 and report.get("ok"):
            if prepare.family == "python":
                metadata = {
                    "python_version": prepare.python.python_version if prepare.python else "",
                    "installed": report.get("installed", [])[:500],
                    "prepare_seconds": report.get("duration_s"),
                }
            else:
                metadata = {"schema": report.get("schema", {}), "datasets": report.get("datasets", ["base"])}
            await self.backend.mark_env_ready(name, metadata, prepare.image)
            env = EnvRef(
                env_hash=prepare.env_hash, image=prepare.image, family=prepare.family, metadata=metadata
            )
            return EnvOutcome(name, env, hit=False, output=output, seconds=output.duration_s, report=report)
        await self.backend.delete_env(name)
        if output.status in (ExecStatus.infra_error, ExecStatus.cancelled):
            return EnvOutcome(
                name, None, hit=False, output=output, seconds=output.duration_s, report=report, error=None
            )
        error = (
            report.get("error")
            or output.error
            or (f"preparation ended with status {output.status.value}, exit code {output.exit_code}")
        )
        if output.status is ExecStatus.timeout:
            error = f"preparing the environment took longer than {sandbox.prepare_timeout_s}s"
        if report.get("missing"):
            error = f"{error}\nmissing package: {report['missing']}"
        return EnvOutcome(
            name,
            None,
            hit=False,
            output=output,
            error=str(error)[:4000],
            seconds=output.duration_s,
            report=report,
        )

    async def _inputs(self, request: ExecRequest) -> dict[str, bytes]:
        prepare = request.prepare
        if prepare.family == "python":
            if prepare.python is None:
                raise InfraError("python prepare spec is missing", retryable=False)
            return {".fixloop/requirements.txt": prepare.python.requirements.encode()}
        if prepare.sql is None:
            raise InfraError("sql prepare spec is missing", retryable=False)
        files: dict[str, bytes] = {}
        seeds = []
        for number, seed in enumerate(prepare.sql.seeds):
            data = await self.store.get_bytes(seed.key)
            if seed.sha256 and sha256_bytes(data) != seed.sha256:
                raise InfraError(f"seed data for {seed.table} does not match its hash", retryable=False)
            path = f".fixloop/seeds/{number}.{seed.format}"
            files[path] = data
            seeds.append({"table": seed.table, "path": path, "format": seed.format})
        spec = {
            "dialect": prepare.sql.dialect,
            "ddl": prepare.sql.ddl,
            "seeds": seeds,
            "mutations": prepare.sql.mutations,
            "mutation_seed": prepare.sql.mutation_seed,
        }
        files[".fixloop/sql_prepare.json"] = json.dumps(spec).encode()
        return files
