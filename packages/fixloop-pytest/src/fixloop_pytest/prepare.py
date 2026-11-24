"""Build a Python environment inside the prepare sandbox (design section 9).

    python -m fixloop_pytest.prepare --venv /opt/venv --requirements reqs.txt \
        --index-url http://pkgproxy:8080/simple --report /out/prepare.json

Wheels only (`--only-binary :all:`): an sdist can run arbitrary code in its build script at
install time, while a wheel is only unpacked. The only network route is the package proxy.
pytest is added when the requirements do not pin it, so every environment can run the oracle.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

_PYTEST_LINE = re.compile(r"^\s*pytest\s*($|[=<>!~;\[ ])", re.I | re.M)
# Sandbox images ship pytest and its dependencies as wheels, so a project without third-party
# dependencies prepares offline in about a second.
BUILTIN_WHEELHOUSE = os.environ.get("FIXLOOP_WHEELHOUSE", "/opt/fixloop/wheelhouse")


def _only_pytest(requirements: str) -> bool:
    lines = [line.split("#", 1)[0].strip() for line in requirements.splitlines()]
    lines = [line for line in lines if line]
    return all(_PYTEST_LINE.match(line) for line in lines)


def _run(cmd: list[str], env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout, check=False)


def _missing_package(stderr: str) -> str | None:
    for pattern in (
        r"Because (\S+?)(?:==\S+)? was not found",
        r"No solution found when resolving.*?\n.*?(\S+)",
        r"(\S+) has no wheels",
        r"not found in the package registry.*?(\S+)",
    ):
        match = re.search(pattern, stderr)
        if match:
            return match.group(1).strip("'\".,")
    return None


def _index_args(index_url: str) -> list[str]:
    """An http(s) index, or a local folder of wheels (a "wheelhouse") for offline development."""
    folder = index_url[len("file://") :] if index_url.startswith("file://") else index_url
    if not index_url.startswith(("http://", "https://")) and os.path.isdir(folder):
        return ["--no-index", "--find-links", folder]
    return ["--index-url", index_url]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fixloop_pytest.prepare")
    parser.add_argument("--venv", required=True)
    parser.add_argument("--requirements", required=True)
    parser.add_argument("--index-url", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--uv", default=os.environ.get("FIXLOOP_UV") or shutil.which("uv") or "uv")
    parser.add_argument("--timeout", type=int, default=540)
    args = parser.parse_args(argv)

    started = time.monotonic()
    report: dict[str, object] = {"ok": False, "installed": [], "error": None, "missing": None}
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        in (
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "TZ",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
            "SSL_CERT_FILE",
            "REQUESTS_CA_BUNDLE",
            "UV_NATIVE_TLS",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
        )
    }
    env.setdefault("HOME", tempfile.gettempdir())
    env["UV_CACHE_DIR"] = os.environ.get("UV_CACHE_DIR") or os.path.join(tempfile.gettempdir(), "uv-cache")
    env["UV_NO_CONFIG"] = "1"
    env["UV_PYTHON_DOWNLOADS"] = "never"
    env["UV_LINK_MODE"] = "copy"  # the cache (tmpfs) and the environment (a volume) are different filesystems
    try:
        with open(args.requirements, encoding="utf-8") as handle:
            requirements = handle.read()
        if not _PYTEST_LINE.search(requirements):
            requirements += "\npytest>=7\n"
        reqs_path = os.path.join(tempfile.gettempdir(), "fixloop-requirements.txt")
        with open(reqs_path, "w", encoding="utf-8") as handle:
            handle.write(requirements)
        venv = _run([args.uv, "venv", args.venv, "--python", args.python, "--no-project"], env, 120)
        if venv.returncode != 0:
            raise RuntimeError("creating the virtual environment failed: " + venv.stderr[-2000:])
        python = (
            os.path.join(args.venv, "Scripts", "python.exe")
            if os.name == "nt"
            else os.path.join(args.venv, "bin", "python")
        )
        index_args = _index_args(args.index_url)
        if _only_pytest(requirements) and os.path.isdir(BUILTIN_WHEELHOUSE):
            index_args = ["--no-index", "--find-links", BUILTIN_WHEELHOUSE]
        install_cmd = [
            args.uv,
            "pip",
            "install",
            "--python",
            python,
            *index_args,
            "--only-binary",
            ":all:",
            "--no-progress",
            "-r",
            reqs_path,
        ]
        install = _run(install_cmd, env, args.timeout)
        if install.returncode != 0:
            report["missing"] = _missing_package(install.stderr)
            raise RuntimeError(
                "installing dependencies failed (wheels only, from the allowlisted proxy):\n"
                + install.stderr[-3000:]
            )
        freeze = _run([args.uv, "pip", "freeze", "--python", python], env, 120)
        report["installed"] = [line for line in freeze.stdout.splitlines() if line.strip()][:2000]
        report["ok"] = True
    except Exception as exc:
        report["error"] = str(exc)[-4000:]
    report["duration_s"] = round(time.monotonic() - started, 2)
    os.makedirs(os.path.dirname(args.report) or ".", exist_ok=True)
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
