"""Local subprocess backend: runs sandbox commands directly on this machine.

UNSAFE. There is no isolation beyond a private working directory, resource limits (on Linux and
macOS), a scrubbed environment and, when the runner is started as root, a separate unprivileged
user. It exists so the test suite and a first try on your own code need nothing but Python.

It refuses to start unless both `FIXLOOP_RUNNER__BACKEND=local` and
`FIXLOOP_RUNNER__ALLOW_UNSAFE_LOCAL=true` are set, and never when `FIXLOOP_ENV=production`
(ADR-11). Use the Docker backend for anything you did not write yourself.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import structlog

from fixloop_core.models import ExecStatus, Limits
from fixloop_core.settings import RunnerSettings
from fixloop_runner.backends.base import BASE_ENV, CappedBuffer, EnvIndex, RunOutput, RunSpec

log = structlog.get_logger(__name__)
IS_WINDOWS = os.name == "nt"
PROXY_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "UV_NATIVE_TLS",
)
WINDOWS_VARS = (
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS",
)


def _set_limits(limits: Limits, family: str) -> None:  # runs in the child, after the user switch
    import resource

    resource.setrlimit(resource.RLIMIT_NOFILE, (limits.nofile, limits.nofile))
    size = limits.fsize_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))
    cpu = max(10, int(limits.timeout_s * limits.cpus) + 5)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if family == "python" and sys.platform.startswith("linux"):
        # Address space, not resident memory: generous on purpose (threads reserve a lot of it).
        memory = limits.memory_mb * 1024 * 1024 * 3
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))


def _chown_tree(root: Path, uid: int, gid: int) -> None:
    for folder, dirs, files in os.walk(root):
        os.chown(folder, uid, gid)
        for name in dirs + files:
            os.lchown(os.path.join(folder, name), uid, gid)


def _make_readable(root: Path) -> None:
    for folder, _dirs, files in os.walk(root):
        os.chmod(folder, 0o755)  # noqa: S103 - the sandbox user must be able to read it
        for name in files:
            path = os.path.join(folder, name)
            mode = os.stat(path).st_mode
            os.chmod(path, 0o755 if mode & stat.S_IXUSR else 0o644)


class LocalBackend:
    name = "local"

    def __init__(self, settings: RunnerSettings) -> None:
        if not settings.allow_unsafe_local:
            raise RuntimeError(
                "the local backend runs code without isolation; set "
                "FIXLOOP_RUNNER__ALLOW_UNSAFE_LOCAL=true to use it (never in production)"
            )
        self.settings = settings
        self.root = Path(settings.workdir).resolve()
        self.execs = self.root / "execs"
        self.envs = self.root / "envs"
        self.tools = self.root / "tools"
        for folder in (self.root, self.execs, self.envs, self.tools):
            folder.mkdir(parents=True, exist_ok=True)
        self.drop = not IS_WINDOWS and os.geteuid() == 0
        self.uid = settings.local_uid
        self.index = EnvIndex(self.root / "env-index.json")
        self.uv = self._install_tools()
        if not IS_WINDOWS:
            for folder in (self.root, self.execs, self.envs):
                os.chmod(folder, 0o755)  # noqa: S103 - the sandbox user must be able to read it
            _make_readable(self.tools)

    # ------------------------------------------------------------------ tools
    def _install_tools(self) -> str:
        """Copy the in-sandbox helpers (pytest plugin, SQL executor, uv) somewhere every user can read."""
        lib = self.tools / "lib"
        lib.mkdir(parents=True, exist_ok=True)
        for module in ("fixloop_pytest", "fixloop_sqlexec"):
            spec = importlib.util.find_spec(module)
            if spec is None or not spec.submodule_search_locations:
                continue
            source = Path(next(iter(spec.submodule_search_locations)))
            target = lib / module
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        uv = shutil.which("uv") or "uv"
        if self.drop and os.path.exists(uv):
            copied = self.tools / "uv"
            if not copied.exists() or copied.stat().st_size != os.path.getsize(uv):
                shutil.copy2(uv, copied)
            os.chmod(copied, 0o755)  # noqa: S103 - the sandbox user must be able to read it
            return str(copied)
        return uv

    def _readable_copy(self, path: str) -> str:
        """A certificate bundle the unprivileged sandbox user can read (it may live under /root)."""
        source = Path(path)
        if not source.is_file():
            return path
        target = self.tools / f"ca-{hashlib.sha256(str(source).encode()).hexdigest()[:8]}.crt"
        if not target.exists() or target.stat().st_mtime < source.stat().st_mtime:
            shutil.copyfile(source, target)
            os.chmod(target, 0o644)
        return str(target)

    def python_for(self, family: str, version: str) -> str:
        pythons = self.settings.local_pythons
        if family == "sql":
            return pythons.get("sql") or pythons.get("default") or sys.executable
        found = pythons.get(version) or shutil.which(f"python{version}") or pythons.get("default")
        if found:
            return found
        if self.drop:
            return (
                "/usr/bin/python3"  # the runner's own interpreter may live where the sandbox user cannot read
            )
        return sys.executable

    # ------------------------------------------------------------------ backend API
    def placeholders(self, family: str, env_name: str | None, image: str, exec_id: str) -> dict[str, str]:
        exec_dir = self.execs / exec_id
        env_dir = self.envs / (env_name or f"none-{exec_id}")
        version = image.split("-", 1)[1] if image.startswith("python-") else ""
        venv_python = env_dir / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
        return {
            "PYTHON": str(venv_python),
            "SYSPYTHON": self.python_for(family, version),
            "WORKSPACE": str(exec_dir / "workspace"),
            "OUT": str(exec_dir / "out"),
            "ENV": str(env_dir),
            "INDEX_URL": self.settings.index_url,
        }

    async def env_metadata(self, env_name: str) -> dict[str, Any] | None:
        entry = self.index.get(env_name)
        if entry is None or not (self.envs / env_name).is_dir():
            return None
        await self.index.touch(env_name)
        return entry.get("metadata", {})

    async def mark_env_ready(self, env_name: str, metadata: dict[str, Any], image: str) -> None:
        env_dir = self.envs / env_name
        if self.drop:
            # Give the prepared environment back to root: later runs can read it but not change it.
            await asyncio.to_thread(_chown_tree, env_dir, 0, 0)
            await asyncio.to_thread(_make_readable, env_dir)
        await self.index.put(env_name, metadata, image)

    async def delete_env(self, env_name: str) -> None:
        await self.index.remove(env_name)
        await asyncio.to_thread(shutil.rmtree, self.envs / env_name, True)

    async def close(self) -> None:
        return None

    def _environment(self, spec: RunSpec, exec_dir: Path, placeholders: dict[str, str]) -> dict[str, str]:
        tmp = exec_dir / "tmp"
        env = dict(BASE_ENV)
        env.update(spec.environment)
        env.update(
            {
                "HOME": str(tmp),
                "TMPDIR": str(tmp),
                "TEMP": str(tmp),
                "TMP": str(tmp),
                "PYTHONPATH": str(self.tools / "lib"),
                "FIXLOOP_UV": self.uv,
                "UV_CACHE_DIR": str(self.root / "uv-cache") if not self.drop else str(tmp / "uv-cache"),
            }
        )
        bins = [str(Path(placeholders["PYTHON"]).parent), str(Path(placeholders["SYSPYTHON"]).parent)]
        if IS_WINDOWS:
            for name in WINDOWS_VARS:
                if name in os.environ:
                    env[name] = os.environ[name]
            env["USERPROFILE"] = env["APPDATA"] = env["LOCALAPPDATA"] = str(tmp)
            env["PATH"] = os.pathsep.join([*bins, os.environ.get("PATH", "")])
        else:
            env["PATH"] = os.pathsep.join([*bins, "/usr/local/bin", "/usr/bin", "/bin"])
        if spec.network == "proxy":
            for name in PROXY_VARS:
                if name in os.environ:
                    env[name] = os.environ[name]
            for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
                if self.drop and name in env:
                    env[name] = self._readable_copy(env[name])
        if self.settings.pg_bin_dir:
            env["FIXLOOP_PG_BIN"] = self.settings.pg_bin_dir
        return env

    async def run(self, spec: RunSpec) -> RunOutput:
        exec_dir = self.execs / spec.exec_id
        workspace, out = exec_dir / "workspace", exec_dir / "out"
        await asyncio.to_thread(self._prepare_dirs, spec, exec_dir)
        placeholders = self.placeholders(spec.family, spec.env_name, spec.image, spec.exec_id)
        env_dir = Path(placeholders["ENV"])
        if spec.env_name and not spec.env_writable and not env_dir.is_dir():  # noqa: ASYNC240 - one stat
            return RunOutput(
                ExecStatus.infra_error, None, 0.0, error="the environment is missing on this runner"
            )
        kwargs: dict[str, Any] = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True
            kwargs["preexec_fn"] = lambda: _set_limits(spec.limits, spec.family)
            if self.drop:
                kwargs.update(user=self.uid, group=self.uid, extra_groups=[])
        started = time.monotonic()
        deadline = started + spec.limits.timeout_s
        if spec.deadline_ts:
            deadline = min(deadline, started + max(1.0, spec.deadline_ts - time.time()))
        stdout, stderr = CappedBuffer(spec.limits.stdout_bytes), CappedBuffer(spec.limits.stdout_bytes)
        try:
            proc = await asyncio.create_subprocess_exec(
                *spec.argv,
                cwd=str(workspace),
                env=self._environment(spec, exec_dir, placeholders),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
        except OSError as exc:
            await self._cleanup(exec_dir)
            return RunOutput(ExecStatus.infra_error, None, 0.0, error=f"could not start the command: {exc}")

        async def pump(stream: asyncio.StreamReader | None, buffer: CappedBuffer) -> None:
            if stream is None:
                return
            while chunk := await stream.read(65536):
                buffer.write(chunk)

        readers = [
            asyncio.create_task(pump(proc.stdout, stdout)),
            asyncio.create_task(pump(proc.stderr, stderr)),
        ]
        status = ExecStatus.completed
        while proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except TimeoutError:
                pass
            if proc.returncode is not None:
                break
            if time.monotonic() > deadline:
                status = ExecStatus.timeout
            elif spec.cancel_check is not None and await spec.cancel_check():
                status = ExecStatus.cancelled
            if status is not ExecStatus.completed:
                self._kill(proc.pid)
                await proc.wait()
        self._kill_strays(exec_dir)
        await asyncio.wait(readers, timeout=5)
        duration = time.monotonic() - started
        files, truncated = await asyncio.to_thread(self._collect, out, spec.limits.artifacts_bytes)
        protected = await asyncio.to_thread(self._hash_protected, workspace, spec.protected_paths)
        await self._cleanup(exec_dir)
        return RunOutput(
            status=status,
            exit_code=proc.returncode,
            duration_s=duration,
            stdout=stdout.value(),
            stderr=stderr.value(),
            files=files,
            truncated={"stdout": stdout.truncated, "stderr": stderr.truncated, "artifacts": truncated},
            protected_hashes=protected,
            image_digest="local",
        )

    # ------------------------------------------------------------------ helpers
    def _prepare_dirs(self, spec: RunSpec, exec_dir: Path) -> None:
        if exec_dir.exists():
            shutil.rmtree(exec_dir)
        workspace = exec_dir / "workspace"
        for folder in (workspace, exec_dir / "out", exec_dir / "tmp"):
            folder.mkdir(parents=True)
        for path, data in spec.workspace.files.items():
            target = workspace / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            if path in spec.workspace.executable and not IS_WINDOWS:
                os.chmod(target, 0o755)  # noqa: S103 - the sandbox user must be able to read it
        if spec.env_name and spec.env_writable:
            env_dir = self.envs / spec.env_name
            if env_dir.exists():
                shutil.rmtree(env_dir)
            env_dir.mkdir()
            if self.drop:
                os.chown(env_dir, self.uid, self.uid)
        if self.drop:
            _chown_tree(exec_dir, self.uid, self.uid)

    def _kill(self, pid: int) -> None:
        try:
            if IS_WINDOWS:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False)
            else:
                os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _kill_strays(self, exec_dir: Path) -> None:
        """Kill processes that left the process group (pg_ctl daemonizes Postgres, for example)."""
        proc_root = Path("/proc")
        if not proc_root.is_dir():
            return
        marker = str(exec_dir).encode()
        for entry in proc_root.iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            if marker in cmdline:
                try:
                    os.kill(int(entry.name), signal.SIGKILL)
                except OSError:
                    pass

    @staticmethod
    def _collect(out: Path, cap: int) -> tuple[dict[str, bytes], bool]:
        files: dict[str, bytes] = {}
        total, truncated = 0, False
        for folder, _dirs, names in os.walk(out):
            for name in sorted(names):
                path = os.path.join(folder, name)
                info = os.lstat(path)
                if not stat.S_ISREG(info.st_mode):
                    continue  # never follow a link the sandbox created
                if total + info.st_size > cap:
                    truncated = True
                    continue
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "rb") as handle:
                    files[Path(path).relative_to(out).as_posix()] = handle.read()
                total += info.st_size
        return files, truncated

    @staticmethod
    def _hash_protected(workspace: Path, paths: list[str]) -> dict[str, str]:
        hashes: dict[str, str] = {}
        root = os.path.realpath(workspace)
        for path in paths:
            target = os.path.realpath(workspace / path)
            if (
                not target.startswith(root + os.sep)
                or not os.path.isfile(target)
                or os.path.islink(workspace / path)
            ):
                hashes[path] = "missing"
                continue
            with open(target, "rb") as handle:
                hashes[path] = hashlib.sha256(handle.read()).hexdigest()
        return hashes

    async def _cleanup(self, exec_dir: Path) -> None:
        if not self.settings.keep_exec_dirs:
            await asyncio.to_thread(shutil.rmtree, exec_dir, True)

    async def reap(self, startup: bool = False) -> int:
        removed = 0
        cutoff = time.time() - 3600
        for entry in list(self.execs.iterdir()):
            if startup or entry.stat().st_mtime < cutoff:
                self._kill_strays(entry)
                await asyncio.to_thread(shutil.rmtree, entry, True)
                removed += 1
        ttl = time.time() - self.settings.env_ttl_days * 86400
        for name, entry in self.index.names().items():
            if entry.get("last_used", 0) < ttl:
                await self.delete_env(name)
                removed += 1
        for folder in list(self.envs.iterdir()):
            # Half-prepared leftovers. A recent one may be a preparation that is still running.
            if (
                folder.is_dir()
                and self.index.get(folder.name) is None
                and (startup or folder.stat().st_mtime < cutoff)
            ):
                await asyncio.to_thread(shutil.rmtree, folder, True)
                removed += 1
        return removed
