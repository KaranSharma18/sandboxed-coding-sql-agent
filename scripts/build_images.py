"""Build the sandbox images (and optionally the services image) with the local Docker daemon.

    uv run poe images                  # every sandbox image
    uv run python scripts/build_images.py --only python-3.12 --services

Works the same on Windows (Docker Desktop), macOS and Linux. Tags match
config/command_templates.yaml, so the runner finds them without any extra configuration.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = {
    **{
        f"python-{v}": (
            "images/sandbox/python/Dockerfile",
            f"ghcr.io/fixloop/sbx-python:{v}",
            {"PYTHON_VERSION": v},
        )
        for v in ("3.10", "3.11", "3.12", "3.13")
    },
    "sql-duckdb": ("images/sandbox/duckdb/Dockerfile", "ghcr.io/fixloop/sbx-duckdb:2", {}),
    "sql-postgres": ("images/sandbox/postgres/Dockerfile", "ghcr.io/fixloop/sbx-postgres:16", {}),
}
SERVICES = ("images/services/Dockerfile", "fixloop/services:dev", {})


def build(dockerfile: str, tag: str, args: dict[str, str]) -> None:
    command = ["docker", "build", "-f", dockerfile, "-t", tag]
    for key, value in args.items():
        command += ["--build-arg", f"{key}={value}"]
    command.append(".")
    print(f"==> {tag}", flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", action="append", choices=sorted(SANDBOX), help="build only these images")
    parser.add_argument("--services", action="store_true", help="also build the services image")
    args = parser.parse_args()
    if shutil.which("docker") is None:
        print(
            "docker was not found: install Docker Desktop (Windows/macOS) or Docker Engine (Linux)",
            file=sys.stderr,
        )
        return 1
    for name in args.only or sorted(SANDBOX):
        build(*SANDBOX[name])
    if args.services:
        build(*SERVICES)
    return 0


if __name__ == "__main__":
    sys.exit(main())
