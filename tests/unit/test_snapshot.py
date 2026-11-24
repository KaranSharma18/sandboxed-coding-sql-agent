import io
import tarfile

import pytest

from fixloop_core.errors import InvalidInput
from fixloop_core.snapshot import SnapshotLimits, build_snapshot, load_snapshot, snapshot_from_archive


def make_tar(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "sym":
                info.type = tarfile.SYMTYPE
                info.linkname = data
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_snapshot_is_deterministic_and_excludes_junk():
    files = {
        "b.py": b"x = 1\n",
        "a.py": b"y = 2\r\n",
        ".git/config": b"[core]",
        "pkg/__pycache__/a.pyc": b"\0",
    }
    first, second = build_snapshot(dict(files)), build_snapshot(dict(reversed(list(files.items()))))
    assert first.sha256 == second.sha256
    assert set(first.files) == {"a.py", "b.py"}
    entries = first.manifest.by_path()
    assert entries["a.py"].eol == "crlf" and entries["b.py"].eol == "lf"


def test_roundtrip_through_storage_form():
    build = build_snapshot({"x.py": b"print(1)\n"})
    again = load_snapshot(build.tar_zst, build.sha256)
    assert again.sha256 == build.sha256 and again.files == build.files


@pytest.mark.parametrize("name", ["/etc/passwd", "../escape.py", "a/../../b.py"])
def test_unsafe_paths_are_refused(name):
    with pytest.raises(InvalidInput):
        snapshot_from_archive(make_tar([(name, b"x", "file")]))


def test_symlinks_are_dropped():
    build = snapshot_from_archive(make_tar([("ok.py", b"x=1\n", "file"), ("link.py", "/etc/passwd", "sym")]))
    assert set(build.files) == {"ok.py"} and any("link.py" in n for n in build.notes)


def test_limits_are_enforced():
    with pytest.raises(InvalidInput):
        snapshot_from_archive(
            make_tar([(f"f{i}.py", b"x", "file") for i in range(5)]), SnapshotLimits(max_files=3)
        )
    with pytest.raises(InvalidInput):
        snapshot_from_archive(
            make_tar([("big.py", b"x" * 5000, "file")]), SnapshotLimits(max_unpacked_bytes=1000)
        )


def test_secrets_are_found_and_key_files_flagged():
    build = build_snapshot({"settings.py": b'TOKEN = "ghp_' + b"a" * 36 + b'"\n', ".env": b"A=1\n"})
    assert build.manifest.secret_files == [".env"]
    assert build.manifest.redactions and build.manifest.redactions[0].kind == "github_token"
