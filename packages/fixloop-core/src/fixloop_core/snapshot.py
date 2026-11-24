"""Content-addressed snapshots of a task's source.

Whatever the source (inline files, an uploaded tarball, a GitHub tarball), the worker turns it
into one normalized snapshot: a sorted tar of regular files with no `.git` directory, no device
files and no symlinks, plus a manifest of every file's size, hash and line-ending style.

The sha256 of the uncompressed tar is the snapshot's identity. It pins exactly what was tested
and is part of every cache key. The tar is deterministic (sorted names, fixed owner, fixed
timestamp), so the same files always produce the same hash.
"""

from __future__ import annotations

import io
import stat
import tarfile
from dataclasses import dataclass, field
from typing import Literal

import zstandard

from fixloop_core.errors import InvalidInput
from fixloop_core.hashing import sha256_bytes
from fixloop_core.models import ManifestEntry, RedactionNote, SnapshotManifest
from fixloop_core.secrets import is_secret_file, scan_text
from fixloop_core.spec import validate_repo_path

SANDBOX_UID = 10001
FIXED_MTIME = 1577836800  # 2020-01-01: deterministic tars without zero timestamps confusing tools

EXCLUDED_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".ruff_cache",
    ".nox",
    ".fixloop",
    ".hg",
    ".svn",
    ".idea",
    ".eggs",
}
EXCLUDED_SUFFIXES = (".pyc", ".pyo")
EXCLUDED_NAMES = {".DS_Store", "Thumbs.db"}


@dataclass
class SnapshotLimits:
    max_files: int = 20_000
    max_unpacked_bytes: int = 500 * 1024 * 1024
    max_file_bytes: int = 100 * 1024 * 1024


@dataclass
class SnapshotBuild:
    files: dict[str, bytes]
    executable: set[str]
    tar_zst: bytes
    sha256: str
    manifest: SnapshotManifest
    notes: list[str] = field(default_factory=list)

    def text(self, path: str) -> str | None:
        """Decoded text of a file, or None for binary/non-UTF-8 files."""
        data = self.files.get(path)
        if data is None:
            return None
        entry = self.manifest.by_path().get(path)
        if entry is not None and entry.binary:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None


def _excluded(path: str) -> bool:
    parts = path.split("/")
    if any(part in EXCLUDED_DIRS for part in parts[:-1]):
        return True
    name = parts[-1]
    return name in EXCLUDED_NAMES or name.endswith(EXCLUDED_SUFFIXES)


def detect_eol(data: bytes) -> Literal["lf", "crlf", "mixed", "none"]:
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    if crlf and lf:
        return "mixed"
    if crlf:
        return "crlf"
    if lf:
        return "lf"
    return "none"


def is_binary(data: bytes) -> bool:
    head = data[:8192]
    if b"\x00" in head:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def write_tar(files: dict[str, bytes], executable: set[str] | None = None) -> bytes:
    """Deterministic tar: sorted names, sandbox-owned, fixed mtime, regular files only."""
    executable = executable or set()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(files):
            data = files[path]
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            info.mtime = FIXED_MTIME
            info.uid = info.gid = SANDBOX_UID
            info.uname = info.gname = "sandbox"
            info.mode = 0o755 if path in executable else 0o644
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def compress(data: bytes, level: int = 10) -> bytes:
    return zstandard.ZstdCompressor(level=level).compress(data)


def decompress_any(data: bytes, max_bytes: int) -> bytes:
    """Return the raw tar bytes of a .tar, .tar.gz or .tar.zst archive, refusing oversized output."""
    if data[:4] == b"\x28\xb5\x2f\xfd":
        reader = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data))
        out = reader.read(max_bytes + 1)
    elif data[:2] == b"\x1f\x8b":
        import gzip

        with gzip.GzipFile(fileobj=io.BytesIO(data)) as handle:
            out = handle.read(max_bytes + 1)
    else:
        out = data[: max_bytes + 1]
    if len(out) > max_bytes:
        raise InvalidInput("archive unpacks to more than the allowed size")
    return out


def read_tar_members(
    tar_bytes: bytes,
    limits: SnapshotLimits,
    strip_components: int = 0,
) -> tuple[dict[str, bytes], set[str], list[str]]:
    """Read regular files from a tar *without touching the disk*, applying every safety rule."""
    files: dict[str, bytes] = {}
    executable: set[str] = set()
    notes: list[str] = []
    total = 0
    try:
        tar = tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:")  # noqa: SIM115 - closed by `with tar` below
    except tarfile.TarError as exc:
        raise InvalidInput(f"not a readable tar archive: {exc}") from exc
    with tar:
        for member in tar:
            name = member.name
            if strip_components:
                parts = name.split("/", strip_components)
                if len(parts) <= strip_components:
                    continue
                name = parts[-1]
            while name.startswith("./"):
                name = name[2:]
            if not name or name.endswith("/"):
                continue
            if member.isdir():
                continue
            if member.issym() or member.islnk():
                notes.append(f"dropped link {name}")
                continue
            if not member.isfile():
                raise InvalidInput(f"archive contains a special file: {name}")
            if name.startswith("/") or ".." in name.split("/") or "\\" in name or "\x00" in name:
                raise InvalidInput(f"archive contains an unsafe path: {name!r}")
            try:
                validate_repo_path(name)
            except ValueError as exc:
                raise InvalidInput(str(exc)) from exc
            if _excluded(name):
                continue
            if member.size > limits.max_file_bytes:
                raise InvalidInput(f"file too large: {name} ({member.size} bytes)")
            total += member.size
            if total > limits.max_unpacked_bytes:
                raise InvalidInput("archive unpacks to more than the allowed size")
            if len(files) >= limits.max_files:
                raise InvalidInput(f"archive has more than {limits.max_files} files")
            handle = tar.extractfile(member)
            files[name] = handle.read() if handle else b""
            if member.mode & stat.S_IXUSR:
                executable.add(name)
    return files, executable, notes


def build_snapshot(
    files: dict[str, bytes], executable: set[str] | None = None, notes: list[str] | None = None
) -> SnapshotBuild:
    """Normalize files into a snapshot and its manifest (including secret findings)."""
    executable = executable or set()
    kept = {path: data for path, data in files.items() if not _excluded(path)}
    for path in kept:
        validate_repo_path(path)
    entries: list[ManifestEntry] = []
    redactions: list[RedactionNote] = []
    secret_files: list[str] = []
    for path in sorted(kept):
        data = kept[path]
        binary = is_binary(data)
        entries.append(
            ManifestEntry(
                path=path,
                size=len(data),
                sha256=sha256_bytes(data),
                eol=detect_eol(data) if not binary else "none",
                binary=binary,
                executable=path in executable,
            )
        )
        if is_secret_file(path):
            secret_files.append(path)
        elif not binary and len(data) <= 2 * 1024 * 1024:
            for hit in scan_text(data.decode("utf-8")):
                redactions.append(RedactionNote(path=path, kind=hit.kind, line=hit.line))
    raw_tar = write_tar(kept, executable & set(kept))
    digest = sha256_bytes(raw_tar)
    manifest = SnapshotManifest(
        snapshot_sha256=digest,
        entries=entries,
        redactions=redactions,
        secret_files=secret_files,
        excluded=sorted(set(files) - set(kept)),
    )
    return SnapshotBuild(
        files=kept,
        executable=executable & set(kept),
        tar_zst=compress(raw_tar),
        sha256=digest,
        manifest=manifest,
        notes=list(notes or []),
    )


def snapshot_from_archive(
    archive: bytes, limits: SnapshotLimits | None = None, strip_components: int = 0
) -> SnapshotBuild:
    limits = limits or SnapshotLimits()
    raw = decompress_any(archive, limits.max_unpacked_bytes + 10 * 1024 * 1024)
    files, executable, notes = read_tar_members(raw, limits, strip_components=strip_components)
    if not files:
        raise InvalidInput("the archive contains no files")
    return build_snapshot(files, executable, notes)


def load_snapshot(
    tar_zst: bytes, expected_sha256: str | None = None, limits: SnapshotLimits | None = None
) -> SnapshotBuild:
    """Rebuild a snapshot from its stored form, verifying the hash (used when resuming a task)."""
    limits = limits or SnapshotLimits()
    raw = decompress_any(tar_zst, limits.max_unpacked_bytes + 10 * 1024 * 1024)
    if expected_sha256 and sha256_bytes(raw) != expected_sha256:
        raise InvalidInput("snapshot hash mismatch: the stored snapshot is corrupt")
    files, executable, notes = read_tar_members(raw, limits)
    return build_snapshot(files, executable, notes)


def github_strip_components(files_tar: bytes) -> int:
    """GitHub tarballs wrap everything in one `owner-repo-sha/` directory; strip it."""
    with tarfile.open(fileobj=io.BytesIO(files_tar), mode="r:") as tar:
        tops = {m.name.split("/", 1)[0] for m in tar.getmembers() if m.name and m.name != "pax_global_header"}
    return 1 if len(tops) == 1 else 0
