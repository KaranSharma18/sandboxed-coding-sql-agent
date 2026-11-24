"""Packing a local repository into a task source (inline files or an uploaded archive)."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    "dist",
    "build",
    ".idea",
    ".vscode",
    ".fixloop-data",
}
SKIP_SUFFIXES = (".pyc", ".pyo", ".so", ".dll", ".dylib", ".exe", ".zip", ".whl", ".tar", ".gz")
INLINE_MAX_BYTES = 1_500_000
INLINE_MAX_FILES = 150


def list_files(root: Path) -> list[str]:
    """Repository files as POSIX paths: `git ls-files` when it is a git repository, else a filtered walk."""
    root = root.resolve()
    git = shutil.which("git")
    if git and (root / ".git").exists():
        result = subprocess.run(
            [git, "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            names = [n for n in result.stdout.decode("utf-8", errors="replace").split("\x00") if n]
            return sorted(n for n in names if (root / n).is_file() and not n.endswith(SKIP_SUFFIXES))
    found: list[str] = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info"))
        for name in sorted(files):
            if name.endswith(SKIP_SUFFIXES):
                continue
            path = Path(folder, name)
            found.append(path.relative_to(root).as_posix())
    return found


def read_text_files(root: Path, paths: list[str]) -> dict[str, str] | None:
    """All files as text, or None if any is binary or not UTF-8 (then an archive is needed)."""
    files: dict[str, str] = {}
    for path in paths:
        data = (root / path).read_bytes()
        if b"\x00" in data[:8192]:
            return None
        try:
            files[path] = data.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return files


def make_archive(root: Path, paths: list[str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path in paths:
            full = root / path
            info = tar.gettarinfo(str(full), arcname=path)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with open(full, "rb") as handle:
                tar.addfile(info, handle)
    return buffer.getvalue()


def source_for(root: Path, upload) -> dict:
    """An inline source for small text-only repositories, otherwise an uploaded .tar.gz."""
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"{root} is not a directory")
    paths = list_files(root)
    if not paths:
        raise ValueError(f"no files found under {root}")
    total = sum((root / p).stat().st_size for p in paths)
    if total <= INLINE_MAX_BYTES and len(paths) <= INLINE_MAX_FILES:
        files = read_text_files(root, paths)
        if files is not None:
            return {"type": "inline", "files": files}
    return {"type": "upload", "upload_id": upload(make_archive(root, paths), "repo", "application/gzip")}
