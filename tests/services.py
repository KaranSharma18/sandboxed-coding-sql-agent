"""Start throwaway Postgres and Redis servers for integration tests.

If FIXLOOP_TEST_DATABASE_URL / FIXLOOP_TEST_REDIS_URL / FIXLOOP_TEST_REDIS_CACHE_URL are set (CI
service containers, or `docker compose up` on a laptop) those are used. Otherwise the local
`redis-server` and Postgres binaries are started in a temporary directory, as an unprivileged
user when the tests run as root (Postgres refuses to run as root).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

PG_BIN_CANDIDATES = [
    "/usr/lib/postgresql/17/bin",
    "/usr/lib/postgresql/16/bin",
    "/usr/lib/postgresql/15/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def pg_bin() -> str | None:
    if os.environ.get("FIXLOOP_PG_BIN"):
        return os.environ["FIXLOOP_PG_BIN"]
    for candidate in PG_BIN_CANDIDATES:
        if Path(candidate, "initdb").exists() and Path(candidate, "pg_ctl").exists():
            return candidate
    return None


def _as_user() -> list[str]:
    if os.name != "nt" and os.geteuid() == 0 and shutil.which("setpriv"):
        return ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups"]
    return []


@dataclass
class Services:
    database_url: str
    redis_url: str
    redis_cache_url: str
    _stop: list = field(default_factory=list)

    def stop(self) -> None:
        for action in reversed(self._stop):
            try:
                action()
            except Exception:
                pass


def start_redis(stop: list) -> str:
    port = free_port()
    proc = subprocess.Popen(
        ["redis-server", "--port", str(port), "--save", "", "--appendonly", "no", "--bind", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    stop.append(proc.terminate)
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return f"redis://127.0.0.1:{port}/0"
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("redis-server did not start")


def start_postgres(stop: list) -> str:
    binary = pg_bin()
    if binary is None:
        raise RuntimeError("Postgres binaries not found")
    root = tempfile.mkdtemp(prefix="fixloop-pg-")
    os.chmod(root, 0o777)
    data = os.path.join(root, "data")
    port = free_port()
    prefix = _as_user()
    subprocess.run(
        [
            *prefix,
            f"{binary}/initdb",
            "-D",
            data,
            "-U",
            "fixloop",
            "--auth=trust",
            "-E",
            "UTF8",
            "--no-instructions",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            *prefix,
            f"{binary}/pg_ctl",
            "-D",
            data,
            "-l",
            os.path.join(root, "log"),
            "-w",
            "-o",
            f"-p {port} -k {root} -c listen_addresses=127.0.0.1 -c fsync=off -c max_connections=200",
            "start",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    stop.append(
        lambda: subprocess.run(
            [*prefix, f"{binary}/pg_ctl", "-D", data, "-m", "immediate", "stop"],
            stdout=subprocess.DEVNULL,
            check=False,
        )
    )
    stop.append(lambda: shutil.rmtree(root, ignore_errors=True))
    subprocess.run(
        [
            f"{binary}/psql",
            "-h",
            "127.0.0.1",
            "-p",
            str(port),
            "-U",
            "fixloop",
            "-d",
            "postgres",
            "-c",
            "CREATE DATABASE fixloop",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return f"postgresql+asyncpg://fixloop@127.0.0.1:{port}/fixloop"


def start_services() -> Services:
    stop: list = []
    try:
        database = os.environ.get("FIXLOOP_TEST_DATABASE_URL") or start_postgres(stop)
        redis_url = os.environ.get("FIXLOOP_TEST_REDIS_URL") or start_redis(stop)
        cache_url = os.environ.get("FIXLOOP_TEST_REDIS_CACHE_URL") or (
            start_redis(stop) if not os.environ.get("FIXLOOP_TEST_REDIS_URL") else redis_url
        )
    except Exception:
        for action in reversed(stop):
            action()
        raise
    return Services(database, redis_url, cache_url, stop)


def migrate(database_url: str) -> None:
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
