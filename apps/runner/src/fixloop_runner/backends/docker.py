"""Docker backend: one short-lived, locked-down container per execution (design section 9).

Every run gets fresh named volumes for the workspace and the output, filled and read through the
Docker archive API. Nothing on the runner's own disk is bind-mounted into a sandbox, which is
also why this works when the runner itself runs in a container (Docker Desktop on Windows).

Hardening applied to every container, whatever the runtime:
    no network (or only the package proxy network, for preparation), read-only root filesystem,
    all capabilities dropped, no-new-privileges, a seccomp profile, a non-root user, memory and
    swap limits, CPU and PID limits, file-size and open-file limits, a small tmpfs, capped logs.
In production the runtime is gVisor (`runsc`), so the code also faces a user-space kernel
instead of the host kernel (ADR-12).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import tarfile
import time
from pathlib import Path
from typing import Any

import structlog

from fixloop_core.errors import InfraError
from fixloop_core.models import ExecStatus
from fixloop_core.settings import RunnerSettings
from fixloop_runner.backends.base import (
    BASE_ENV,
    CappedBuffer,
    EnvIndex,
    RunOutput,
    RunSpec,
    output_files_from_tar,
)

log = structlog.get_logger(__name__)
SANDBOX_USER = "10001:10001"
ENV_MOUNTS = {"python": "/opt/venv", "sql": "/env"}


class DockerBackend:
    name = "docker"

    def __init__(self, settings: RunnerSettings, client: Any | None = None) -> None:
        import docker

        self.settings = settings
        self.client = client or (
            docker.DockerClient(base_url=settings.docker_host, timeout=120)
            if settings.docker_host
            else docker.from_env(timeout=120)
        )
        if settings.isolation == "production" and settings.runtime != "runsc":
            raise RuntimeError("production isolation requires FIXLOOP_RUNNER__RUNTIME=runsc (gVisor)")
        self.runtime = settings.runtime
        self.index = EnvIndex(Path(settings.workdir) / "env-index.json")
        self.seccomp = (
            Path(settings.seccomp_profile).read_text(encoding="utf-8") if settings.seccomp_profile else None
        )
        self._digests: dict[str, tuple[str, float]] = {}

    # ------------------------------------------------------------------ backend API
    def placeholders(self, family: str, env_name: str | None, image: str, exec_id: str) -> dict[str, str]:
        return {
            "PYTHON": "/opt/venv/bin/python",
            "SYSPYTHON": "python3",
            "WORKSPACE": "/workspace",
            "OUT": "/out",
            "ENV": ENV_MOUNTS.get(family, "/env"),
            "INDEX_URL": self.settings.index_url,
        }

    async def env_metadata(self, env_name: str) -> dict[str, Any] | None:
        entry = self.index.get(env_name)
        if entry is None:
            return None
        if not await asyncio.to_thread(self._volume_exists, env_name):
            await self.index.remove(env_name)
            return None
        await self.index.touch(env_name)
        return entry.get("metadata", {})

    async def mark_env_ready(self, env_name: str, metadata: dict[str, Any], image: str) -> None:
        await self.index.put(env_name, metadata, image)

    async def delete_env(self, env_name: str) -> None:
        await self.index.remove(env_name)
        await asyncio.to_thread(self._remove_volume, env_name)

    async def close(self) -> None:
        await asyncio.to_thread(self.client.close)

    async def run(self, spec: RunSpec) -> RunOutput:
        started = time.monotonic()
        try:
            return await self._run(spec, started)
        except InfraError as exc:
            return RunOutput(ExecStatus.infra_error, None, time.monotonic() - started, error=str(exc))
        except Exception as exc:  # docker API errors: the daemon restarted, a volume is busy, ...
            log.exception("docker_run_failed", exec_id=spec.exec_id)
            return RunOutput(
                ExecStatus.infra_error,
                None,
                time.monotonic() - started,
                error=f"sandbox infrastructure error: {type(exc).__name__}: {str(exc)[:500]}",
            )

    # ------------------------------------------------------------------ the run
    def _labels(self, spec: RunSpec, role: str) -> dict[str, str]:
        return {
            "fixloop.role": role,
            "fixloop.runner": self.settings.runner_id,
            "fixloop.exec": spec.exec_id,
            "fixloop.tenant": spec.tenant_id,
            "fixloop.purpose": spec.purpose,
            "fixloop.deadline": str(int(spec.deadline_ts or time.time() + spec.limits.timeout_s)),
        }

    def _host_config(self, spec: RunSpec, ws_volume: str, out_volume: str) -> dict[str, Any]:
        from docker.types import LogConfig, Mount, Ulimit

        limits = spec.limits
        mounts = [Mount("/workspace", ws_volume, type="volume"), Mount("/out", out_volume, type="volume")]
        if spec.env_name:
            mounts.append(
                Mount(
                    ENV_MOUNTS.get(spec.family, "/env"),
                    spec.env_name,
                    type="volume",
                    read_only=not spec.env_writable,
                )
            )
        security = ["no-new-privileges:true"]
        if self.seccomp:
            security.append(f"seccomp={self.seccomp}")
        fsize = limits.fsize_mb * 1024 * 1024
        config: dict[str, Any] = {
            "mounts": mounts,
            "network_mode": "none" if spec.network == "none" else self.settings.proxy_network,
            "read_only": True,
            "tmpfs": {"/tmp": f"rw,nosuid,nodev,size={limits.tmpfs_mb}m,mode=1777"},  # noqa: S108 - in the sandbox
            "cap_drop": ["ALL"],
            "security_opt": security,
            "pids_limit": limits.pids,
            "mem_limit": f"{limits.memory_mb}m",
            "memswap_limit": f"{limits.memory_mb}m",
            "nano_cpus": int(limits.cpus * 1_000_000_000),
            "shm_size": "64m",
            "ulimits": [
                Ulimit(name="nofile", soft=limits.nofile, hard=limits.nofile),
                Ulimit(name="fsize", soft=fsize, hard=fsize),
                Ulimit(name="core", soft=0, hard=0),
            ],
            "log_config": LogConfig(
                type="json-file",
                config={"max-size": f"{max(64, limits.stdout_bytes // 1024)}k", "max-file": "1"},
            ),
        }
        if self.runtime:
            config["runtime"] = self.runtime
        return config

    async def _run(self, spec: RunSpec, started: float) -> RunOutput:
        image_digest = await asyncio.to_thread(self._ensure_image, spec.image_ref)
        ws_volume, out_volume = f"fixloop-ws-{spec.exec_id}", f"fixloop-out-{spec.exec_id}"
        await asyncio.to_thread(self._create_volume, ws_volume, self._labels(spec, "sandbox-volume"))
        await asyncio.to_thread(self._create_volume, out_volume, self._labels(spec, "sandbox-volume"))
        if spec.env_name and spec.env_writable:
            await asyncio.to_thread(self._remove_volume, spec.env_name)
            await asyncio.to_thread(
                self._create_volume,
                spec.env_name,
                {**self._labels(spec, "env"), "fixloop.env": spec.env_name},
            )
        elif spec.env_name and not await asyncio.to_thread(self._volume_exists, spec.env_name):
            raise InfraError("the environment volume is missing on this runner")
        environment = {
            **BASE_ENV,
            **spec.environment,
            "HOME": "/tmp",  # noqa: S108 - paths inside the sandbox
            "TMPDIR": "/tmp",  # noqa: S108
            "PYTHONPATH": "/opt/fixloop/lib",
        }
        container = None
        try:
            container = await asyncio.to_thread(
                self.client.containers.create,
                spec.image_ref,
                command=spec.argv,
                user=SANDBOX_USER,
                working_dir="/workspace",
                environment=environment,
                labels=self._labels(spec, "sandbox"),
                hostname="sandbox",
                network_disabled=spec.network == "none",
                stop_signal="SIGKILL",
                **self._host_config(spec, ws_volume, out_volume),
            )
            await asyncio.to_thread(container.put_archive, "/workspace", spec.workspace.to_tar())
            run_started = time.monotonic()
            await asyncio.to_thread(container.start)
            status = await self._wait(container, spec, run_started)
            duration = time.monotonic() - run_started
            await asyncio.to_thread(container.reload)
            state = container.attrs.get("State", {})
            exit_code = state.get("ExitCode")
            if status is ExecStatus.completed and state.get("OOMKilled"):
                status = ExecStatus.oom
            elif status is ExecStatus.completed and exit_code == 137:
                status = ExecStatus.killed
            stdout, stderr = await asyncio.to_thread(self._logs, container, spec.limits.stdout_bytes)
            files, truncated = await asyncio.to_thread(self._read_out, container, spec.limits.artifacts_bytes)
            protected = await asyncio.to_thread(self._hash_protected, container, spec.protected_paths)
            return RunOutput(
                status=status,
                exit_code=exit_code,
                duration_s=duration,
                stdout=stdout.value(),
                stderr=stderr.value(),
                files=files,
                truncated={"stdout": stdout.truncated, "stderr": stderr.truncated, "artifacts": truncated},
                protected_hashes=protected,
                image_digest=image_digest,
            )
        finally:
            if container is not None:
                await asyncio.to_thread(self._remove_container, container)
            await asyncio.to_thread(self._remove_volume, ws_volume)
            await asyncio.to_thread(self._remove_volume, out_volume)

    async def _wait(self, container: Any, spec: RunSpec, run_started: float) -> ExecStatus:
        deadline = run_started + spec.limits.timeout_s
        if spec.deadline_ts:
            deadline = min(deadline, run_started + max(1.0, spec.deadline_ts - time.time()))
        last_cancel_check = 0.0
        while True:
            await asyncio.to_thread(container.reload)
            if container.status in ("exited", "dead"):
                return ExecStatus.completed
            now = time.monotonic()
            if now > deadline:
                await asyncio.to_thread(self._kill, container)
                return ExecStatus.timeout
            if spec.cancel_check is not None and now - last_cancel_check >= 1.0:
                last_cancel_check = now
                if await spec.cancel_check():
                    await asyncio.to_thread(self._kill, container)
                    return ExecStatus.cancelled
            await asyncio.sleep(0.25)

    # ------------------------------------------------------------------ docker helpers (blocking)
    def _ensure_image(self, ref: str) -> str:
        import docker.errors

        cached = self._digests.get(ref)
        if cached and time.time() - cached[1] < 300:
            return cached[0]
        try:
            image = self.client.images.get(ref)
        except docker.errors.ImageNotFound:
            try:
                image = self.client.images.pull(ref)
            except docker.errors.APIError as exc:
                raise InfraError(
                    f"sandbox image {ref} is not available (build it with `uv run poe images`): "
                    f"{str(exc)[:200]}",
                    retryable=False,
                ) from exc
        digests = image.attrs.get("RepoDigests") or []
        digest = str(digests[0] if digests else image.id)
        self._digests[ref] = (digest, time.time())
        return digest

    def _volume_exists(self, name: str) -> bool:
        import docker.errors

        try:
            self.client.volumes.get(name)
            return True
        except docker.errors.NotFound:
            return False

    def _create_volume(self, name: str, labels: dict[str, str]) -> None:
        self.client.volumes.create(name=name, driver="local", labels=labels)

    def _remove_volume(self, name: str) -> None:
        import docker.errors

        for _ in range(5):
            try:
                self.client.volumes.get(name).remove(force=True)
                return
            except docker.errors.NotFound:
                return
            except docker.errors.APIError:
                time.sleep(0.5)  # still in use by a container that is being removed

    def _kill(self, container: Any) -> None:
        import docker.errors

        try:
            container.kill()
        except docker.errors.APIError:
            pass

    def _remove_container(self, container: Any) -> None:
        import docker.errors

        try:
            container.remove(force=True, v=True)
        except docker.errors.NotFound:
            pass
        except docker.errors.APIError as exc:
            log.warning("container_remove_failed", error=str(exc)[:200])

    @staticmethod
    def _logs(container: Any, cap: int) -> tuple[CappedBuffer, CappedBuffer]:
        stdout, stderr = CappedBuffer(cap), CappedBuffer(cap)
        stdout.write(container.logs(stdout=True, stderr=False) or b"")
        stderr.write(container.logs(stdout=False, stderr=True) or b"")
        return stdout, stderr

    @staticmethod
    def _archive_bytes(container: Any, path: str, cap: int) -> bytes:
        stream, _ = container.get_archive(path)
        buffer = io.BytesIO()
        for chunk in stream:
            buffer.write(chunk)
            if buffer.tell() > cap + 10 * 1024 * 1024:
                break  # the tar is cut short; files read so far are still usable
        return buffer.getvalue()

    def _read_out(self, container: Any, cap: int) -> tuple[dict[str, bytes], bool]:
        data = self._archive_bytes(container, "/out", cap)
        try:
            return output_files_from_tar(data, "out", cap)
        except tarfile.ReadError:
            return {}, True

    def _hash_protected(self, container: Any, paths: list[str]) -> dict[str, str]:
        import docker.errors

        hashes: dict[str, str] = {}
        for path in paths[:200]:
            try:
                data = self._archive_bytes(container, f"/workspace/{path}", 50 * 1024 * 1024)
            except docker.errors.NotFound:
                hashes[path] = "missing"
                continue
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
                member = next((m for m in tar if m.isreg()), None)
                handle = tar.extractfile(member) if member is not None else None
                hashes[path] = hashlib.sha256(handle.read()).hexdigest() if handle is not None else "missing"
        return hashes

    # ------------------------------------------------------------------ reaping
    async def reap(self, startup: bool = False) -> int:
        removed, dropped = await asyncio.to_thread(self._reap, startup)
        for name in dropped:
            await self.index.remove(name)
        return removed

    def _reap(self, startup: bool) -> tuple[int, list[str]]:
        import docker.errors

        removed = 0
        dropped: list[str] = []
        now = time.time()
        mine = f"fixloop.runner={self.settings.runner_id}"
        for container in self.client.containers.list(
            all=True, filters={"label": ["fixloop.role=sandbox", mine]}
        ):
            deadline = float(container.labels.get("fixloop.deadline", "0") or 0)
            if startup or deadline + 120 < now:
                self._remove_container(container)
                removed += 1
        for volume in self.client.volumes.list(filters={"label": ["fixloop.role=sandbox-volume", mine]}):
            deadline = float((volume.attrs.get("Labels") or {}).get("fixloop.deadline", "0") or 0)
            if startup or deadline + 300 < now:
                try:
                    volume.remove(force=True)
                    removed += 1
                except docker.errors.APIError:
                    pass
        ttl = now - self.settings.env_ttl_days * 86400
        indexed = self.index.names()
        for volume in self.client.volumes.list(filters={"label": ["fixloop.role=env", mine]}):
            entry = indexed.get(volume.name)
            created = float((volume.attrs.get("Labels") or {}).get("fixloop.deadline", "0") or 0)
            stale_unindexed = entry is None and (startup or created + 3600 < now)
            if stale_unindexed or (entry is not None and entry.get("last_used", 0) < ttl):
                try:
                    volume.remove(force=True)
                    removed += 1
                except docker.errors.APIError:
                    continue
                if entry is not None:
                    dropped.append(volume.name)
        return removed, dropped
