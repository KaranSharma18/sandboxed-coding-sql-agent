"""DuckDB and Postgres engines: build snapshot databases, compute schema statistics, run guarded queries."""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fixloop_sqlexec.serialize import to_json

STATS_SAMPLE_LIMIT = 5


def _qi(name: str) -> str:
    """Quote an identifier."""
    return '"' + name.replace('"', '""') + '"'


def _max_estimate(node: Any, key: str) -> int:
    best = 0
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                try:
                    best = max(best, int(float(v)))
                except (TypeError, ValueError):
                    pass
            else:
                best = max(best, _max_estimate(v, key))
    elif isinstance(node, list):
        for item in node:
            best = max(best, _max_estimate(item, key))
    return best


# ====================================================================================== DuckDB
class DuckDB:
    dialect = "duckdb"

    def __init__(self, env: str) -> None:
        self.env = env

    def path(self, dataset: str) -> str:
        return os.path.join(self.env, f"{dataset}.duckdb")

    # ------------------------------------------------------------------ prepare
    def prepare(self, spec: dict[str, Any]) -> dict[str, Any]:
        import duckdb

        os.makedirs(self.env, exist_ok=True)
        base = self.path("base")
        if os.path.exists(base):
            os.remove(base)
        con = duckdb.connect(base, config={"threads": 2, "memory_limit": "1GB"})
        try:
            con.execute(spec["ddl"])
            for seed in spec.get("seeds", []):
                table, path = seed["table"], seed["path"]
                if seed.get("format", "csv") == "parquet":
                    con.execute(f"INSERT INTO {_qi(table)} SELECT * FROM read_parquet(?)", [path])
                else:
                    con.execute(
                        f"COPY {_qi(table)} FROM '{path.replace(chr(39), chr(39) * 2)}' "
                        "(FORMAT csv, HEADER true)"
                    )
            schema = self._schema(con)
        finally:
            con.close()
        datasets = ["base"]
        for index in range(1, int(spec.get("mutations", 0)) + 1):
            name = f"mut{index}"
            shutil.copyfile(base, self.path(name))
            con = duckdb.connect(self.path(name))
            try:
                self._mutate(con, schema, index, int(spec.get("mutation_seed", 0)))
            finally:
                con.close()
            datasets.append(name)
        return {"schema": schema, "datasets": datasets}

    def _schema(self, con: Any) -> dict[str, Any]:
        tables = []
        names = [
            r[0]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main' ORDER BY table_name"
            ).fetchall()
        ]
        for table in names:
            columns = con.execute(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'main' AND table_name = ? ORDER BY ordinal_position",
                [table],
            ).fetchall()
            row_count = con.execute(f"SELECT count(*) FROM {_qi(table)}").fetchone()[0]
            described = []
            for name, data_type, nullable in columns:
                described.append(
                    {
                        "name": name,
                        "type": data_type,
                        "nullable": nullable == "YES",
                        "stats": self._column_stats(con, table, name, row_count),
                    }
                )
            tables.append({"name": table, "row_count": row_count, "columns": described})
        return {"tables": tables}

    def _column_stats(self, con: Any, table: str, column: str, rows: int) -> dict[str, Any]:
        q = _qi(column)
        try:
            distinct, nulls, lo, hi = con.execute(
                f"SELECT approx_count_distinct({q}), count(*) - count({q}), "
                f"min({q})::VARCHAR, max({q})::VARCHAR "
                f"FROM {_qi(table)}"
            ).fetchone()
            samples = [
                r[0]
                for r in con.execute(
                    f"SELECT DISTINCT {q}::VARCHAR FROM {_qi(table)} "
                    f"WHERE {q} IS NOT NULL LIMIT {STATS_SAMPLE_LIMIT}"
                ).fetchall()
            ]
        except Exception:
            return {}
        return {
            "distinct": distinct,
            "null_fraction": round(nulls / rows, 4) if rows else 0.0,
            "min": (lo or "")[:60],
            "max": (hi or "")[:60],
            "samples": [str(s)[:60] for s in samples],
        }

    def _mutate(self, con: Any, schema: dict[str, Any], index: int, seed: int) -> None:
        for table in schema["tables"]:
            t = _qi(table["name"])
            salt = f"{seed}-{index}-{table['name']}"
            pick = f"(hash(rowid::VARCHAR || '{salt}') % 10)"
            statements = []
            if index == 1:
                statements.append(f"DELETE FROM {t} WHERE {pick} = 0")
            elif index == 2:
                statements.append(f"INSERT INTO {t} SELECT * FROM {t} WHERE {pick} = 1")
                for column in table["columns"]:
                    if column["nullable"] and not _looks_like_key(column["name"]):
                        statements.append(
                            f"UPDATE {t} SET {_qi(column['name'])} = NULL "
                            f"WHERE (hash(rowid::VARCHAR || '{salt}{column['name']}') % 10) = 2"
                        )
            else:
                for column in table["columns"]:
                    kind = column["type"].upper()
                    name = _qi(column["name"])
                    if _looks_like_key(column["name"]):
                        continue
                    if any(k in kind for k in ("INT", "DECIMAL", "NUMERIC", "DOUBLE", "FLOAT", "REAL")):
                        statements.append(f"UPDATE {t} SET {name} = {name} + 1 WHERE {pick} < 3")
                    elif kind.startswith("DATE") or "TIMESTAMP" in kind:
                        statements.append(f"UPDATE {t} SET {name} = {name} + INTERVAL 1 DAY WHERE {pick} < 3")
            for statement in statements:
                try:
                    con.execute(statement)
                except Exception:  # noqa: S112 - a mutation that does not apply to this table is skipped
                    continue

    # ------------------------------------------------------------------ run
    def run(self, spec: dict[str, Any]) -> dict[str, Any]:
        import duckdb

        out: dict[str, Any] = {"dialect": "duckdb", "datasets": {}}
        for dataset in spec.get("datasets", ["base"]):
            con = duckdb.connect(
                self.path(dataset),
                read_only=True,
                config={"threads": 2, "memory_limit": "1GB", "enable_external_access": False},
            )
            con.execute("SET lock_configuration = true")
            try:
                out["datasets"][dataset] = self._run_dataset(con, spec, dataset)
            finally:
                con.close()
        return out

    def _run_dataset(self, con: Any, spec: dict[str, Any], dataset: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        query = spec["candidate_query"]
        timeout = spec.get("statement_timeout_ms", 10000) / 1000
        if dataset == "base":
            result["explain"] = self._explain(con, query, spec.get("explain_max_rows", 50_000_000))
            if result["explain"].get("too_expensive"):
                return result
        result["candidate"] = self._query(con, query, spec.get("max_rows", 10000), timeout)
        if spec.get("reference_query"):
            result["reference"] = self._query(
                con, spec["reference_query"], spec.get("max_rows", 10000), timeout * 3
            )
        if dataset == "base":
            result["assertions"] = [
                self._assertion(con, query, sql, timeout) for sql in spec.get("assertions", [])
            ]
        return result

    def _explain(self, con: Any, query: str, limit: int) -> dict[str, Any]:
        try:
            rows = con.execute(f"EXPLAIN (FORMAT JSON) {query}").fetchall()
            plan = json.loads(rows[0][1]) if rows else []
            estimate = _max_estimate(plan, "Estimated Cardinality")
            return {
                "max_rows_estimate": estimate,
                "too_expensive": estimate > limit,
                "summary": f"largest estimated row count {estimate}",
            }
        except Exception as exc:
            return {
                "max_rows_estimate": None,
                "too_expensive": False,
                "summary": f"explain failed: {exc}"[:300],
            }

    def _query(self, con: Any, query: str, max_rows: int, timeout: float) -> dict[str, Any]:
        started = time.monotonic()
        box: dict[str, Any] = {}

        def work() -> None:
            try:
                cursor = con.execute(query)
                rows = cursor.fetchmany(max_rows + 1)
                box["columns"] = [d[0] for d in cursor.description or []]
                box["types"] = [str(d[1]) for d in cursor.description or []]
                box["rows"] = rows
            except Exception as exc:
                box["error"] = exc

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            con.interrupt()
            thread.join(5)
            return {"error": {"kind": "timeout", "message": f"the query ran longer than {timeout:g}s"}}
        if "error" in box:
            return {
                "error": {
                    "kind": "runtime",
                    "message": str(box["error"])[:1000],
                    "sqlstate": None,
                    "position": None,
                }
            }
        rows = box["rows"]
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        return {
            "columns": box["columns"],
            "types": box["types"],
            "rows": [[to_json(v) for v in r] for r in rows],
            "truncated": truncated,
            "row_count": len(rows),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "error": None,
        }

    def _assertion(self, con: Any, candidate: str, assertion: str, timeout: float) -> dict[str, Any]:
        wrapped = f"WITH candidate_result AS ({candidate}) {assertion}"
        result = self._query(con, wrapped, 1000, timeout)
        if result.get("error"):
            return {"rows": None, "error": result["error"]["message"]}
        return {"rows": result["row_count"], "error": None}


def _looks_like_key(name: str) -> bool:
    lower = name.lower()
    return lower == "id" or lower.endswith(("_id", "_key"))


# ====================================================================================== Postgres
class Postgres:
    dialect = "postgres"
    ADMIN = "fixloop_admin"
    READER = "fixloop_ro"

    def __init__(self, env: str, work: str | None = None) -> None:
        self.env = env
        self.bin = os.environ.get("FIXLOOP_PG_BIN", "/usr/lib/postgresql/16/bin")
        self.work = work

    def _tool(self, name: str) -> str:
        path = os.path.join(self.bin, name)
        return path if os.path.exists(path) else (shutil.which(name) or name)

    @contextmanager
    def server(self, datadir: str) -> Iterator[str]:
        sock = tempfile.mkdtemp(prefix="fxpg-")
        log = os.path.join(sock, "server.log")
        options = (
            f"-k {sock} -c listen_addresses='' -c fsync=off -c full_page_writes=off "
            "-c synchronous_commit=off -c max_connections=20 -c shared_buffers=32MB -c temp_file_limit=256MB"
        )
        start = subprocess.run(
            [self._tool("pg_ctl"), "-D", datadir, "-l", log, "-o", options, "-w", "-t", "60", "start"],
            capture_output=True,
            text=True,
            check=False,
        )
        if start.returncode != 0:
            detail = ""
            if os.path.exists(log):
                with open(log, encoding="utf-8", errors="replace") as handle:
                    detail = handle.read()[-2000:]
            raise RuntimeError(f"postgres failed to start: {start.stderr[-1000:]} {detail}")
        try:
            yield sock
        finally:
            subprocess.run(
                [self._tool("pg_ctl"), "-D", datadir, "-m", "fast", "-w", "stop"],
                capture_output=True,
                check=False,
            )
            shutil.rmtree(sock, ignore_errors=True)

    def _connect(self, sock: str, dbname: str, user: str) -> Any:
        import psycopg

        return psycopg.connect(host=sock, dbname=dbname, user=user, autocommit=True, connect_timeout=10)

    # ------------------------------------------------------------------ prepare
    def prepare(self, spec: dict[str, Any]) -> dict[str, Any]:
        datadir = os.path.join(self.env, "pgdata")
        if os.path.exists(datadir):
            shutil.rmtree(datadir)
        os.makedirs(self.env, exist_ok=True)
        init = subprocess.run(
            [
                self._tool("initdb"),
                "-D",
                datadir,
                "-U",
                self.ADMIN,
                "--auth=trust",
                "--encoding=UTF8",
                "--locale=C.UTF-8",
                "--no-instructions",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if init.returncode != 0:
            init = subprocess.run(
                [
                    self._tool("initdb"),
                    "-D",
                    datadir,
                    "-U",
                    self.ADMIN,
                    "--auth=trust",
                    "--encoding=UTF8",
                    "--no-locale",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if init.returncode != 0:
                raise RuntimeError("initdb failed: " + init.stderr[-1500:])
        datasets = ["base"]
        with self.server(datadir) as sock:
            admin = self._connect(sock, "postgres", self.ADMIN)
            admin.execute("CREATE DATABASE base")
            admin.execute(f"CREATE ROLE {self.READER} LOGIN")
            # Defense in depth: the reader role is read-only by default, on top of each READ ONLY transaction.
            admin.execute(f"ALTER ROLE {self.READER} SET default_transaction_read_only = on")
            con = self._connect(sock, "base", self.ADMIN)
            with con.transaction():
                con.execute(spec["ddl"])
            for seed in spec.get("seeds", []):
                self._load_seed(con, seed)
            con.execute(f"GRANT CONNECT ON DATABASE base TO {self.READER}")
            con.execute(f"GRANT USAGE ON SCHEMA public TO {self.READER}")
            con.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {self.READER}")
            con.execute("ANALYZE")
            schema = self._schema(con)
            con.close()
            for index in range(1, int(spec.get("mutations", 0)) + 1):
                name = f"mut{index}"
                admin.execute(f"CREATE DATABASE {name} TEMPLATE base")
                mcon = self._connect(sock, name, self.ADMIN)
                self._mutate(mcon, schema, index, int(spec.get("mutation_seed", 0)))
                mcon.execute(f"GRANT CONNECT ON DATABASE {name} TO {self.READER}")
                mcon.execute("ANALYZE")
                mcon.close()
                datasets.append(name)
            admin.close()
        return {"schema": schema, "datasets": datasets}

    def _load_seed(self, con: Any, seed: dict[str, Any]) -> None:
        path = seed["path"]
        if seed.get("format", "csv") == "parquet":
            import duckdb

            csv_path = path + ".csv"
            duckdb.connect().execute(
                f"COPY (SELECT * FROM read_parquet(?)) TO '{csv_path}' (HEADER, FORMAT csv)", [path]
            )
            path = csv_path
        with (
            con.cursor() as cur,
            cur.copy(f"COPY {_qi(seed['table'])} FROM STDIN WITH (FORMAT csv, HEADER true)") as copy,
            open(path, "rb") as handle,
        ):
            while chunk := handle.read(1 << 20):
                copy.write(chunk)

    def _schema(self, con: Any) -> dict[str, Any]:
        tables = []
        names = [
            r[0]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY 1"
            ).fetchall()
        ]
        for table in names:
            columns = con.execute(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
                [table],
            ).fetchall()
            row_count = con.execute(f"SELECT count(*) FROM {_qi(table)}").fetchone()[0]
            described = []
            for name, data_type, nullable in columns:
                q = _qi(name)
                stats: dict[str, Any] = {}
                try:
                    distinct, nulls, lo, hi = con.execute(
                        f"SELECT count(DISTINCT {q}), count(*) - count({q}), min({q})::text, max({q})::text "
                        f"FROM {_qi(table)}"
                    ).fetchone()
                    samples = [
                        r[0]
                        for r in con.execute(
                            f"SELECT DISTINCT {q}::text FROM {_qi(table)} "
                            f"WHERE {q} IS NOT NULL LIMIT {STATS_SAMPLE_LIMIT}"
                        ).fetchall()
                    ]
                    stats = {
                        "distinct": distinct,
                        "null_fraction": round(nulls / row_count, 4) if row_count else 0.0,
                        "min": (lo or "")[:60],
                        "max": (hi or "")[:60],
                        "samples": [str(s)[:60] for s in samples],
                    }
                except Exception:
                    stats = {}
                described.append(
                    {"name": name, "type": data_type, "nullable": nullable == "YES", "stats": stats}
                )
            tables.append({"name": table, "row_count": row_count, "columns": described})
        return {"tables": tables}

    def _mutate(self, con: Any, schema: dict[str, Any], index: int, seed: int) -> None:
        for table in schema["tables"]:
            t = _qi(table["name"])
            salt = f"{seed}-{index}-{table['name']}"
            pick = f"(abs(hashtext(ctid::text || '{salt}')) % 10)"
            statements = []
            if index == 1:
                statements.append(f"DELETE FROM {t} WHERE {pick} = 0")
            elif index == 2:
                statements.append(f"INSERT INTO {t} SELECT * FROM {t} WHERE {pick} = 1")
                for column in table["columns"]:
                    if column["nullable"] and not _looks_like_key(column["name"]):
                        statements.append(
                            f"UPDATE {t} SET {_qi(column['name'])} = NULL "
                            f"WHERE (abs(hashtext(ctid::text || '{salt}{column['name']}')) % 10) = 2"
                        )
            else:
                for column in table["columns"]:
                    kind = column["type"].lower()
                    name = _qi(column["name"])
                    if _looks_like_key(column["name"]):
                        continue
                    if any(k in kind for k in ("int", "numeric", "double", "real", "decimal")):
                        statements.append(f"UPDATE {t} SET {name} = {name} + 1 WHERE {pick} < 3")
                    elif kind.startswith(("date", "timestamp")):
                        statements.append(
                            f"UPDATE {t} SET {name} = {name} + interval '1 day' WHERE {pick} < 3"
                        )
            for statement in statements:
                try:
                    con.execute(statement)
                except Exception:  # noqa: S112 - a mutation that does not apply to this table is skipped
                    continue

    # ------------------------------------------------------------------ run
    def run(self, spec: dict[str, Any]) -> dict[str, Any]:
        work = self.work or tempfile.mkdtemp(prefix="fxpgrun-")
        datadir = os.path.join(work, "pgdata")
        if os.path.exists(datadir):
            shutil.rmtree(datadir)
        # A fresh copy every run: no query ever sees another query's leftovers.
        shutil.copytree(os.path.join(self.env, "pgdata"), datadir, symlinks=True)
        os.chmod(datadir, 0o700)
        for name in ("postmaster.pid", "postmaster.opts"):
            stale = os.path.join(datadir, name)
            if os.path.exists(stale):
                os.remove(stale)
        out: dict[str, Any] = {"dialect": "postgres", "datasets": {}}
        try:
            with self.server(datadir) as sock:
                for dataset in spec.get("datasets", ["base"]):
                    con = self._connect(sock, dataset, self.READER)
                    try:
                        out["datasets"][dataset] = self._run_dataset(con, spec, dataset)
                    finally:
                        con.close()
        finally:
            shutil.rmtree(datadir, ignore_errors=True)
        return out

    def _run_dataset(self, con: Any, spec: dict[str, Any], dataset: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        query = spec["candidate_query"]
        timeout_ms = int(spec.get("statement_timeout_ms", 10000))
        if dataset == "base":
            result["explain"] = self._explain(
                con, query, spec.get("explain_max_rows", 50_000_000), timeout_ms
            )
            if result["explain"].get("too_expensive"):
                return result
        result["candidate"] = self._query(con, query, spec.get("max_rows", 10000), timeout_ms)
        if spec.get("reference_query"):
            result["reference"] = self._query(
                con, spec["reference_query"], spec.get("max_rows", 10000), timeout_ms * 3
            )
        if dataset == "base":
            result["assertions"] = []
            for sql in spec.get("assertions", []):
                res = self._query(con, f"WITH candidate_result AS ({query}) {sql}", 1000, timeout_ms)
                result["assertions"].append(
                    {"rows": None, "error": res["error"]["message"]}
                    if res.get("error")
                    else {"rows": res["row_count"], "error": None}
                )
        return result

    @contextmanager
    def _readonly(self, con: Any, timeout_ms: int) -> Iterator[Any]:
        with con.transaction():
            con.execute("SET TRANSACTION READ ONLY")
            con.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
            con.execute("SET LOCAL work_mem = '64MB'")
            yield con

    def _explain(self, con: Any, query: str, limit: int, timeout_ms: int) -> dict[str, Any]:
        try:
            with self._readonly(con, timeout_ms):
                plan = con.execute(f"EXPLAIN (FORMAT JSON) {query}").fetchone()[0]
            estimate = _max_estimate(plan, "Plan Rows")
            return {
                "max_rows_estimate": estimate,
                "too_expensive": estimate > limit,
                "summary": f"largest estimated row count {estimate}",
            }
        except Exception as exc:
            return {
                "max_rows_estimate": None,
                "too_expensive": False,
                "summary": f"explain failed: {exc}"[:300],
            }

    def _query(self, con: Any, query: str, max_rows: int, timeout_ms: int) -> dict[str, Any]:
        import psycopg

        started = time.monotonic()
        try:
            with self._readonly(con, timeout_ms), con.cursor() as cur:
                cur.execute(query)
                rows = cur.fetchmany(max_rows + 1) if cur.description else []
                columns = [d.name for d in cur.description or []]
                types = []
                for d in cur.description or []:
                    info = psycopg.adapters.types.get(d.type_code)
                    types.append(info.name if info else str(d.type_code))
        except psycopg.Error as exc:
            diag = exc.diag
            position = None
            try:
                position = int(diag.statement_position) if diag.statement_position else None
            except (TypeError, ValueError):
                position = None
            kind = "timeout" if exc.sqlstate == "57014" else "runtime"
            return {
                "error": {
                    "kind": kind,
                    "sqlstate": exc.sqlstate,
                    "message": (diag.message_primary or str(exc))[:1000],
                    "position": position,
                }
            }
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        return {
            "columns": columns,
            "types": types,
            "rows": [[to_json(v) for v in r] for r in rows],
            "truncated": truncated,
            "row_count": len(rows),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "error": None,
        }


def engine_for(dialect: str, env: str, work: str | None = None) -> DuckDB | Postgres:
    if dialect == "duckdb":
        return DuckDB(env)
    if dialect == "postgres":
        return Postgres(env, work)
    raise ValueError(f"unsupported dialect {dialect!r}")


def deterministic_seed(value: Any) -> int:
    return random.Random(str(value)).randint(0, 2**31 - 1)
