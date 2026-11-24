"""Materializing a workspace: snapshot + candidate overlay + platform files, all hash-checked."""

from __future__ import annotations

import base64
import io
import tarfile
from dataclasses import dataclass, field

from fixloop_core.errors import InfraError
from fixloop_core.hashing import sha256_bytes
from fixloop_core.models import ExecRequest
from fixloop_core.objectstore import ObjectStore
from fixloop_core.snapshot import FIXED_MTIME, SANDBOX_UID, SnapshotLimits, decompress_any, read_tar_members


@dataclass
class Workspace:
    files: dict[str, bytes] = field(default_factory=dict)
    executable: set[str] = field(default_factory=set)

    def to_tar(self) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
            directories: set[str] = set()
            for path in sorted(self.files):
                parts = path.split("/")[:-1]
                for depth in range(1, len(parts) + 1):
                    directory = "/".join(parts[:depth])
                    if directory not in directories:
                        directories.add(directory)
                        info = tarfile.TarInfo(directory)
                        info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, FIXED_MTIME
                        info.uid = info.gid = SANDBOX_UID
                        tar.addfile(info)
                data = self.files[path]
                info = tarfile.TarInfo(path)
                info.size, info.mtime = len(data), FIXED_MTIME
                info.mode = 0o755 if path in self.executable else 0o644
                info.uid = info.gid = SANDBOX_UID
                tar.addfile(info, io.BytesIO(data))
        return buffer.getvalue()


async def materialize(
    request: ExecRequest, store: ObjectStore, limits: SnapshotLimits | None = None
) -> Workspace:
    limits = limits or SnapshotLimits()
    workspace = Workspace()
    if request.snapshot is not None:
        blob = await store.get_bytes(request.snapshot.key)
        raw = decompress_any(blob, limits.max_unpacked_bytes + 10 * 1024 * 1024)
        if sha256_bytes(raw) != request.snapshot.sha256:
            raise InfraError("snapshot hash mismatch: refusing to run a corrupted snapshot", retryable=False)
        files, executable, _ = read_tar_members(raw, limits)
        workspace.files, workspace.executable = files, executable
    for overlay in request.overlay:
        if overlay.delete:
            workspace.files.pop(overlay.path, None)
            continue
        data = base64.b64decode(overlay.content_b64)
        if overlay.sha256 and sha256_bytes(data) != overlay.sha256:
            raise InfraError(f"overlay hash mismatch for {overlay.path}", retryable=False)
        workspace.files[overlay.path] = data
    if request.hidden_tests is not None:
        blob = await store.get_bytes(request.hidden_tests.key)
        raw = decompress_any(blob, 50 * 1024 * 1024)
        hidden, _, _ = read_tar_members(
            raw, SnapshotLimits(max_files=2000, max_unpacked_bytes=50 * 1024 * 1024)
        )
        workspace.files.update(hidden)
    for extra in request.extra_files:
        workspace.files[extra.path] = base64.b64decode(extra.content_b64)
    return workspace
