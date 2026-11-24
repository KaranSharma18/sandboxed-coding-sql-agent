"""Turning a task's source into a snapshot, and the inputs fixed once per task (design section 8).

Everything here runs once, at the start of a task, and its results are checkpointed: the
snapshot (content-addressed in the object store), the dependency list, the protected files and
their hashes, the hidden tests. A resumed task never rebuilds them, so it cannot drift.
"""

from __future__ import annotations

import csv
import io
import re
import tomllib
from typing import Any

from fixloop_core.config import AppConfig, normalize_project_name
from fixloop_core.errors import EnvFailed, InvalidInput
from fixloop_core.hashing import hash_json, sha256_bytes, stable_hash
from fixloop_core.models import ArtifactRef, SnapshotRef
from fixloop_core.policy import matches_any, protected_paths_for
from fixloop_core.ports import task_prefix
from fixloop_core.snapshot import (
    SnapshotBuild,
    SnapshotLimits,
    build_snapshot,
    decompress_any,
    github_strip_components,
    read_tar_members,
    snapshot_from_archive,
)
from fixloop_core.spec import ExpectedResult, GitHubSource, InlineSource, PythonOracle, TaskSpec, UploadSource
from fixloop_core.traces import is_test_path

PREPARE_VERSION = "prepare-v1"  # bump when the prepare script changes what an environment contains
SQL_PREPARE_VERSION = "sqlprep-v1"
_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_TEST_GROUPS = ("test", "tests", "testing", "dev")


# ------------------------------------------------------------------------------------ snapshots
async def build_python_snapshot(run: Any) -> SnapshotBuild:
    """Build the snapshot from the task's source: inline files, an uploaded archive or GitHub."""
    source = run.spec.source
    intake = run.config.limits.intake
    limits = SnapshotLimits(max_files=intake.max_files, max_unpacked_bytes=intake.unpacked_max_bytes)
    if isinstance(source, InlineSource):
        return build_snapshot({path: text.encode("utf-8") for path, text in source.files.items()})
    if isinstance(source, UploadSource):
        upload = await run.deps.store.upload(source.upload_id, run.tenant_id)
        if upload is None:
            raise InvalidInput(f"upload {source.upload_id} does not exist")
        archive = await run.deps.blobs.get_bytes(upload.key, max_bytes=intake.upload_max_bytes)
        return snapshot_from_archive(archive, limits)
    if isinstance(source, GitHubSource):
        if run.deps.github is None:
            raise InvalidInput("GitHub sources need the GitHub App to be configured")
        sha = source.resolved_sha or source.ref
        archive = await run.deps.github.tarball(
            source.installation_id, source.repo, sha, intake.upload_max_bytes
        )
        raw = decompress_any(archive, intake.unpacked_max_bytes + 10 * 1024 * 1024)
        files, executable, notes = read_tar_members(
            raw, limits, strip_components=github_strip_components(raw)
        )
        if not files:
            raise InvalidInput("the repository archive is empty")
        return build_snapshot(files, executable, notes)
    raise InvalidInput("python tasks need an inline, upload or github source")


async def store_snapshot(run: Any, build: SnapshotBuild) -> SnapshotRef:
    """Snapshots are content-addressed per tenant, so identical inputs share one object."""
    key = task_prefix(str(run.tenant_id), "snapshots", f"{build.sha256}.tar.zst")
    if not await run.deps.blobs.exists(key):
        await run.deps.blobs.put_bytes(key, build.tar_zst, "application/zstd")
    return SnapshotRef(key=key, sha256=build.sha256, size=len(build.tar_zst), files=len(build.files))


def check_targets_exist(build: SnapshotBuild, spec: TaskSpec) -> None:
    oracle = spec.oracle
    assert isinstance(oracle, PythonOracle)
    missing = sorted({t.split("::", 1)[0] for t in oracle.target_tests} - set(build.files))
    if missing:
        raise InvalidInput("target test file(s) not found in the repository: " + ", ".join(missing[:5]))


# ------------------------------------------------------------------------------------ dependencies
def _decode(data: bytes | None) -> str | None:
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _expand_requirements(
    files: dict[str, bytes], path: str, seen: set[str], warnings: list[str]
) -> list[str]:
    """Inline `-r` / `-c` includes and drop options the sandbox must never honour."""
    if path in seen:
        return []
    seen.add(path)
    text = _decode(files.get(path))
    if text is None:
        raise InvalidInput(f"dependency file {path} is missing or not text")
    folder = path.rsplit("/", 1)[0] + "/" if "/" in path else ""
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-r ", "--requirement ", "-c ", "--constraint ")):
            included = line.split(None, 1)[1].strip()
            lines += _expand_requirements(files, folder + included, seen, warnings)
            continue
        if line in (".", "./") or line.startswith(("-e ", "--editable ", "./", "../", "/")):
            warnings.append(f"ignored local install {line!r} (the repository is on the path already)")
            continue
        if line.startswith(
            (
                "--index-url",
                "-i ",
                "--extra-index-url",
                "--find-links",
                "-f ",
                "--trusted-host",
                "--no-index",
                "--pre",
                "--prefer-binary",
                "--only-binary",
                "--no-binary",
            )
        ):
            warnings.append(f"ignored index option {line.split()[0]!r} (only the package proxy is allowed)")
            continue
        if line.startswith("-"):
            warnings.append(f"ignored option {line!r}")
            continue
        if "://" in line or " @ " in line or line.startswith(("git+", "hg+", "svn+", "bzr+")):
            raise EnvFailed(
                f"dependency {line!r} points to a URL or VCS checkout; only packages from the "
                "allowlisted index can be installed"
            )
        lines.append(line)
    return lines


def _pyproject_requirements(text: str) -> list[str]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise InvalidInput(f"pyproject.toml is not valid TOML: {exc}") from exc
    project = data.get("project", {}) or {}
    requirements = list(project.get("dependencies", []) or [])
    optional = project.get("optional-dependencies", {}) or {}
    groups = data.get("dependency-groups", {}) or {}
    for name in _TEST_GROUPS:  # PEP 621 extras and PEP 735 dependency groups used for testing
        requirements += [r for r in optional.get(name, []) or [] if isinstance(r, str)]
        requirements += [r for r in groups.get(name, []) or [] if isinstance(r, str)]
    return [r.strip() for r in requirements if r and r.strip()]


def detect_requirements(
    files: dict[str, bytes], dependency_file: str | None
) -> tuple[list[str], str, list[str]]:
    """(requirement lines, where they came from, warnings). Order is kept; duplicates are dropped."""
    warnings: list[str] = []
    if dependency_file:
        if dependency_file.endswith(".toml"):
            text = _decode(files.get(dependency_file))
            if text is None:
                raise InvalidInput(f"dependency file {dependency_file} not found")
            lines, origin = _pyproject_requirements(text), dependency_file
        else:
            lines, origin = _expand_requirements(files, dependency_file, set(), warnings), dependency_file
    else:
        lines, origin = [], "none (pytest only)"
        for name in (
            "requirements-test.txt",
            "requirements-dev.txt",
            "test-requirements.txt",
            "requirements.txt",
        ):
            if name in files:
                lines, origin = _expand_requirements(files, name, set(), warnings), name
                break
        else:
            text = _decode(files.get("pyproject.toml"))
            if text is not None:
                lines, origin = _pyproject_requirements(text), "pyproject.toml"
    unique: list[str] = []
    for line in lines:
        if line not in unique:
            unique.append(line)
    return unique, origin, warnings


def check_allowlist(lines: list[str], allowlist: frozenset[str]) -> None:
    refused = []
    for line in lines:
        match = _REQ_NAME.match(line)
        if match and normalize_project_name(match.group(1).split("[", 1)[0]) not in allowlist:
            refused.append(match.group(1))
    if refused:
        raise EnvFailed(
            "these dependencies are not on the package allowlist (config/allowlist.txt): "
            + ", ".join(sorted(set(refused))[:10])
        )


def python_env_hash(config: AppConfig, image: str, python_version: str, requirements: str) -> str:
    return stable_hash(
        PREPARE_VERSION, image, config.pinned_images.get(image, ""), python_version, requirements.strip()
    )


def sql_env_hash(dialect: str, ddl: str, seed_hashes: list[str], mutations: int, mutation_seed: int) -> str:
    return stable_hash(SQL_PREPARE_VERSION, dialect, ddl.strip(), seed_hashes, mutations, mutation_seed)


def oracle_hash(spec: TaskSpec) -> str:
    return hash_json(spec.oracle.model_dump(mode="json"))


# ------------------------------------------------------------------------------------ protection
def protected_files(
    build: SnapshotBuild, spec: TaskSpec, config: AppConfig
) -> tuple[list[str], dict[str, str]]:
    """The protected globs for this task and the hashes of every existing protected file."""
    oracle = spec.oracle
    assert isinstance(oracle, PythonOracle)
    target_files = sorted({t.split("::", 1)[0] for t in oracle.target_tests})
    globs = protected_paths_for(config.policies.patch, oracle.protected_paths, target_files)
    ordered = target_files + sorted(p for p in build.files if p not in target_files)
    hashes: dict[str, str] = {}
    for path in ordered:
        if path in build.files and (path in target_files or matches_any(path, globs)):
            hashes[path] = sha256_bytes(build.files[path])
        if len(hashes) >= 200:
            break
    return globs, hashes


async def load_hidden_tests(run: Any, upload_id: str) -> tuple[ArtifactRef, list[str], dict[str, str]]:
    upload = await run.deps.store.upload(upload_id, run.tenant_id)
    if upload is None:
        raise InvalidInput(f"hidden tests upload {upload_id} does not exist")
    data = await run.deps.blobs.get_bytes(upload.key, max_bytes=50 * 1024 * 1024)
    raw = decompress_any(data, 50 * 1024 * 1024)
    files, _, _ = read_tar_members(raw, SnapshotLimits(max_files=2000, max_unpacked_bytes=50 * 1024 * 1024))
    if not files:
        raise InvalidInput("the hidden tests archive is empty")
    tests = sorted(p for p in files if p.endswith(".py") and is_test_path(p))
    if not tests:
        raise InvalidInput("the hidden tests archive contains no test files")
    hashes = {path: sha256_bytes(content) for path, content in files.items()}
    return ArtifactRef(key=upload.key, sha256=sha256_bytes(data), size=len(data)), tests, hashes


# ------------------------------------------------------------------------------------ sql inputs
def _cell(value: str) -> Any:
    if value == "":
        return None
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            continue
    return value


def parse_expected_csv(data: bytes) -> ExpectedResult:
    text = data.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise InvalidInput("the expected-rows file is empty")
    header, body = rows[0], rows[1:]
    if len(body) > 10_000:
        raise InvalidInput("expected rows are limited to 10,000")
    return ExpectedResult(columns=header, rows=[[_cell(v) for v in row] for row in body if row])
