"""SQL verification (design section 11).

Static checks run in the worker with sqlglot before any sandbox time is spent. Execution happens
only inside the sandbox, on snapshot databases, in read-only transactions; the sandbox returns
result sets and this module compares them. Feedback to the model never contains expected values.
"""

from __future__ import annotations

import difflib
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify

from fixloop_core.config import SqlPolicy
from fixloop_core.models import Verdict, VerificationLevel
from fixloop_core.outputs import FailureClass
from fixloop_core.spec import ExpectedResult, SqlOracle

_FORBIDDEN_NAMES = (
    "Insert",
    "Update",
    "Delete",
    "Merge",
    "Create",
    "Drop",
    "Alter",
    "Command",
    "Copy",
    "Set",
    "Transaction",
    "Commit",
    "Rollback",
    "Grant",
    "Revoke",
    "Use",
    "Pragma",
    "Attach",
    "Detach",
    "Install",
    "Load",
    "LoadData",
    "Analyze",
    "Refresh",
    "TruncateTable",
    "Export",
)
# Built defensively: sqlglot adds and renames expression classes between releases.
FORBIDDEN_NODES: tuple[type[exp.Expr], ...] = tuple(
    getattr(exp, name) for name in _FORBIDDEN_NAMES if isinstance(getattr(exp, name, None), type)
)
_CURRENT_TIME_NODES: tuple[type[exp.Expr], ...] = tuple(
    getattr(exp, name)
    for name in ("CurrentDate", "CurrentTimestamp", "CurrentTime", "CurrentDatetime")
    if isinstance(getattr(exp, name, None), type)
)


@dataclass
class StaticResult:
    ok: bool
    failure_class: FailureClass | None = None
    message: str = ""
    tables: set[str] = field(default_factory=set)


def _function_names(tree: exp.Expr) -> set[str]:
    names: set[str] = set()
    for node in tree.find_all(exp.Func):
        if isinstance(node, exp.Anonymous):
            names.add(str(node.name).lower())
        else:
            names.add(node.sql_name().lower())
            for key in ("this",):
                inner = node.args.get(key)
                if isinstance(inner, exp.Identifier) and isinstance(node, exp.Anonymous):
                    names.add(inner.name.lower())
    for table in tree.find_all(exp.Table):  # table functions such as read_csv('x') in FROM
        if isinstance(table.this, exp.Func):
            names.add(
                table.this.sql_name().lower()
                if not isinstance(table.this, exp.Anonymous)
                else str(table.this.name).lower()
            )
    return names


def static_check(
    query: str, dialect: str, schema: dict[str, dict[str, str]], policy: SqlPolicy, as_of: date | None = None
) -> StaticResult:
    if len(query) > policy.max_query_chars:
        return StaticResult(
            False, FailureClass.sql_policy, f"the query is longer than {policy.max_query_chars} characters"
        )
    try:
        statements = [s for s in sqlglot.parse(query, read=dialect) if s is not None]
    except ParseError as exc:
        return StaticResult(
            False, FailureClass.sql_parse_error, f"the query does not parse: {str(exc)[:300]}"
        )
    if len(statements) != 1:
        return StaticResult(
            False, FailureClass.sql_policy, f"exactly one statement is allowed, got {len(statements)}"
        )
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        return StaticResult(
            False, FailureClass.sql_policy, "only SELECT queries (optionally with WITH) are allowed"
        )
    bad = next((node for node in tree.walk() if isinstance(node, FORBIDDEN_NODES)), None)
    if bad is not None:
        return StaticResult(
            False, FailureClass.sql_policy, f"{type(bad).__name__} is not allowed inside a query"
        )
    functions = _function_names(tree)
    denied = functions & policy.denied_for(dialect)
    if denied:
        return StaticResult(
            False, FailureClass.sql_policy, f"these functions are not allowed: {', '.join(sorted(denied))}"
        )
    if as_of is not None:
        timed = functions & {f.lower() for f in policy.time_functions}
        if timed or (_CURRENT_TIME_NODES and tree.find(*_CURRENT_TIME_NODES)):
            return StaticResult(
                False,
                FailureClass.sql_policy,
                f"use the literal date '{as_of.isoformat()}' instead of current-time functions",
            )
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    tables = {t.name.lower() for t in tree.find_all(exp.Table) if t.name and not isinstance(t.this, exp.Func)}
    real_tables = tables - cte_names
    if not real_tables:
        return StaticResult(
            False, FailureClass.hardcoded_result, "the query reads no table; results must come from the data"
        )
    known = {name.lower(): cols for name, cols in schema.items()}
    unknown = sorted(t for t in real_tables if t not in known)
    if unknown:
        hints = [
            f"{t} (did you mean {', '.join(difflib.get_close_matches(t, known, n=2)) or 'nothing similar'}?)"
            for t in unknown
        ]
        return StaticResult(
            False, FailureClass.sql_unknown_identifier, "unknown table(s): " + "; ".join(hints), real_tables
        )
    try:
        qualify(
            tree.copy(),
            dialect=dialect,
            schema={k: dict(v) for k, v in known.items()},
            validate_qualify_columns=True,
            quote_identifiers=False,
            identify=False,
        )
    except OptimizeError as exc:
        message = str(exc)
        column = message.split("'")[1] if message.count("'") >= 2 else ""
        all_columns = {c for cols in known.values() for c in cols}
        similar = difflib.get_close_matches(column.lower(), all_columns, n=3) if column else []
        hint = f" Did you mean {', '.join(similar)}?" if similar else ""
        return StaticResult(
            False, FailureClass.sql_unknown_identifier, f"{message[:300]}.{hint}", real_tables
        )
    except Exception as exc:
        return StaticResult(True, None, f"static qualification skipped: {type(exc).__name__}", real_tables)
    return StaticResult(True, tables=real_tables)


# ------------------------------------------------------------------------------------ result comparison
def column_kind(type_name: str) -> str:
    upper = (type_name or "").upper()
    if any(token in upper for token in ("INT", "DECIMAL", "NUMERIC", "DOUBLE", "FLOAT", "REAL", "NUMBER")):
        return "number"
    if "BOOL" in upper:
        return "bool"
    if "TIMESTAMP" in upper or "DATETIME" in upper:
        return "timestamp"
    if upper.startswith("DATE"):
        return "date"
    return "text"


def _digits(tolerance: float) -> int:
    if tolerance <= 0:
        return 12
    return max(0, min(12, round(-math.log10(tolerance))))


def normalize_value(value: Any, kind: str, oracle: SqlOracle) -> Any:
    if value is None or (value == "" and kind != "text"):
        return None
    if kind == "number":
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            return ("text", str(value))
        if number.is_nan():
            return ("nan",)
        return ("num", number.quantize(Decimal(1).scaleb(-_digits(oracle.float_tolerance))).normalize())
    if kind == "bool":
        text = str(value).strip().lower()
        if text in ("true", "t", "1", "yes"):
            return ("bool", True)
        if text in ("false", "f", "0", "no"):
            return ("bool", False)
        return ("text", text)
    if kind in ("date", "timestamp"):
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(UTC).replace(tzinfo=None)
            return ("dt", parsed.date().isoformat() if kind == "date" else parsed.isoformat())
        except ValueError:
            return ("text", text)
    text = value if isinstance(value, str) else str(value)
    if oracle.trim_strings:
        text = text.strip()
    if oracle.casefold_strings:
        text = text.casefold()
    return ("text", text)


@dataclass
class ResultSet:
    columns: list[str]
    types: list[str]
    rows: list[list[Any]]
    truncated: bool = False

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ResultSet:
        return cls(
            columns=[str(c) for c in data.get("columns", [])],
            types=[str(t) for t in data.get("types", [])],
            rows=[list(r) for r in data.get("rows", [])],
            truncated=bool(data.get("truncated")),
        )


@dataclass
class Comparison:
    match: bool
    message: str
    failure_class: FailureClass | None = None
    expected_rows: int = 0
    actual_rows: int = 0
    missing: int = 0
    extra: int = 0
    extra_examples: list[list[Any]] = field(default_factory=list)
    differing_columns: list[str] = field(default_factory=list)


def _clean(name: str) -> str:
    return name.strip().strip('"').strip("`").casefold()


def compare_results(
    expected_columns: list[str],
    expected_rows: list[list[Any]],
    expected_types: list[str] | None,
    actual: ResultSet,
    oracle: SqlOracle,
) -> Comparison:
    if actual.truncated:
        return Comparison(
            False,
            "the result was truncated at the row limit, so it cannot be compared in full",
            FailureClass.sql_wrong_rows,
            len(expected_rows),
            len(actual.rows),
        )
    # Map expected columns onto actual columns.
    if oracle.match_columns_by == "name":
        index = {_clean(c): i for i, c in enumerate(actual.columns)}
        mapping, missing_cols = [], []
        for column in expected_columns:
            position = index.get(_clean(column))
            if position is None:
                missing_cols.append(column)
            else:
                mapping.append(position)
        extra_cols = [c for c in actual.columns if _clean(c) not in {_clean(e) for e in expected_columns}]
        if missing_cols or (extra_cols and not oracle.allow_extra_columns):
            parts = []
            if missing_cols:
                parts.append(f"missing column(s): {', '.join(missing_cols)}")
            if extra_cols and not oracle.allow_extra_columns:
                parts.append(f"unexpected column(s): {', '.join(extra_cols)}")
            return Comparison(
                False,
                "wrong columns - " + "; ".join(parts) + f". Your query returned: {', '.join(actual.columns)}",
                FailureClass.sql_wrong_columns,
                len(expected_rows),
                len(actual.rows),
            )
    else:
        if len(actual.columns) < len(expected_columns) or (
            len(actual.columns) > len(expected_columns) and not oracle.allow_extra_columns
        ):
            return Comparison(
                False,
                f"wrong number of columns: expected {len(expected_columns)}, got {len(actual.columns)}",
                FailureClass.sql_wrong_columns,
                len(expected_rows),
                len(actual.rows),
            )
        mapping = list(range(len(expected_columns)))
    kinds = [column_kind(actual.types[i]) if i < len(actual.types) else "text" for i in mapping]
    if expected_types:
        kinds = [k if k != "text" else column_kind(t) for k, t in zip(kinds, expected_types, strict=False)]
    projected = [[row[i] if i < len(row) else None for i in mapping] for row in actual.rows]
    norm_actual = [
        tuple(normalize_value(v, k, oracle) for v, k in zip(row, kinds, strict=False)) for row in projected
    ]
    norm_expected = [
        tuple(normalize_value(v, k, oracle) for v, k in zip(row, kinds, strict=False))
        for row in expected_rows
    ]

    if oracle.order_matters:
        ok = _ordered_equal(norm_expected, norm_actual, expected_columns, oracle.sort_key)
        if ok:
            return Comparison(True, "rows match (in order)", None, len(norm_expected), len(norm_actual))
    exp_counter, act_counter = Counter(norm_expected), Counter(norm_actual)
    missing = exp_counter - act_counter
    extra = act_counter - exp_counter
    if not missing and not extra and not oracle.order_matters:
        return Comparison(True, "rows match", None, len(norm_expected), len(norm_actual))
    if not oracle.order_matters and (missing or extra) and oracle.relative_tolerance > 0:
        missing, extra = _tolerant_pairing(missing, extra, oracle.relative_tolerance)
        if not missing and not extra:
            return Comparison(True, "rows match within tolerance", None, len(norm_expected), len(norm_actual))
    differing = []
    for position, column in enumerate(expected_columns):
        if Counter(r[position] for r in norm_expected) != Counter(r[position] for r in norm_actual):
            differing.append(column)
    extra_examples = []
    extra_set = set(extra)
    for raw, normalized in zip(projected, norm_actual, strict=False):
        if normalized in extra_set and len(extra_examples) < 3:
            extra_examples.append(raw)
    n_missing, n_extra = sum(missing.values()), sum(extra.values())
    if oracle.order_matters and not n_missing and not n_extra:
        message = "the right rows, but in the wrong order"
    else:
        message = (
            f"wrong rows: expected {len(norm_expected)} row(s), got {len(norm_actual)}; "
            f"{n_missing} expected row(s) missing, {n_extra} unexpected row(s)"
        )
        if differing:
            message += f"; values differ in column(s): {', '.join(differing)}"
    return Comparison(
        False,
        message,
        FailureClass.sql_wrong_rows,
        len(norm_expected),
        len(norm_actual),
        n_missing,
        n_extra,
        extra_examples,
        differing,
    )


def _ordered_equal(
    expected: list[tuple], actual: list[tuple], columns: list[str], sort_key: list[str]
) -> bool:
    if len(expected) != len(actual):
        return False
    if not sort_key:
        return expected == actual
    key_idx = [i for i, c in enumerate(columns) if _clean(c) in {_clean(k) for k in sort_key}]
    if not key_idx:
        return expected == actual

    def groups(rows: list[tuple]) -> list[Counter]:
        out: list[Counter] = []
        previous = object()
        for row in rows:
            key = tuple(row[i] for i in key_idx)
            if key != previous:
                out.append(Counter())
                previous = key
            out[-1][row] += 1
        return out

    return groups(expected) == groups(actual)


def _num(value: Any) -> Decimal | None:
    return value[1] if isinstance(value, tuple) and value and value[0] == "num" else None


def _tolerant_pairing(missing: Counter, extra: Counter, rel: float) -> tuple[Counter, Counter]:
    missing_rows, extra_rows = list(missing.elements()), list(extra.elements())
    if len(missing_rows) > 200 or len(extra_rows) > 200:
        return missing, extra
    unmatched_extra = list(extra_rows)
    still_missing = []
    for row in missing_rows:
        found = None
        for candidate in unmatched_extra:
            if len(candidate) == len(row) and all(
                _close(a, b, rel) for a, b in zip(row, candidate, strict=False)
            ):
                found = candidate
                break
        if found is None:
            still_missing.append(row)
        else:
            unmatched_extra.remove(found)
    return Counter(still_missing), Counter(unmatched_extra)


def _close(a: Any, b: Any, rel: float) -> bool:
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return abs(na - nb) <= Decimal(str(rel)) * max(abs(na), abs(nb), Decimal(1))
    return a == b


# ------------------------------------------------------------------------------------ properties
def evaluate_properties(
    oracle: SqlOracle, actual: ResultSet, assertions: list[dict[str, Any]]
) -> list[tuple[Any, bool, str]]:
    results: list[tuple[Any, bool, str]] = []
    index = {_clean(c): i for i, c in enumerate(actual.columns)}
    kinds = [column_kind(t) for t in actual.types]

    def col(name: str) -> int | None:
        return index.get(_clean(name))

    assertion_iter = iter(assertions)
    for prop in oracle.properties:
        kind = prop.kind
        ok, why = True, ""
        if kind == "columns":
            names = [_clean(n) for n in prop.names]  # type: ignore[union-attr]
            ok = [_clean(c) for c in actual.columns] == names
            why = f"columns are {', '.join(actual.columns)}"
            types = prop.types  # type: ignore[union-attr]
            if ok and types:
                ok = all(t.lower() in a.lower() for t, a in zip(types, actual.types, strict=False))
                why = f"column types are {', '.join(actual.types)}"
        elif kind == "row_count":
            n = len(actual.rows)
            v, v2 = prop.value, prop.value2  # type: ignore[union-attr]
            ok = {
                "eq": n == v,
                "ge": n >= v,
                "le": n <= v,
                "between": v <= n <= (v2 if v2 is not None else v),
            }[prop.op]  # type: ignore[union-attr]
            why = "row count check failed"
        elif kind == "unique":
            idx = [col(c) for c in prop.columns]  # type: ignore[union-attr]
            if None in idx:
                ok, why = False, "a uniqueness column is missing"
            else:
                keys = [tuple(r[i] for i in idx) for r in actual.rows]  # type: ignore[index]
                ok = len(keys) == len(set(map(repr, keys)))
                why = f"rows are not unique on {', '.join(prop.columns)}"  # type: ignore[union-attr]
        elif kind == "not_null":
            i = col(prop.column)  # type: ignore[union-attr]
            ok = i is not None and all(r[i] is not None for r in actual.rows)
            why = f"column {prop.column} contains NULL or is missing"  # type: ignore[union-attr]
        elif kind == "values_in":
            i = col(prop.column)  # type: ignore[union-attr]
            allowed = {normalize_value(v, kinds[i] if i is not None else "text", oracle) for v in prop.values}  # type: ignore[union-attr]
            ok = i is not None and all(
                normalize_value(r[i], kinds[i], oracle) in allowed for r in actual.rows
            )
            why = "values check failed"
        elif kind == "sorted_by":
            found = [col(c) for c in prop.columns]  # type: ignore[union-attr]
            idx = [i for i in found if i is not None]
            if len(idx) != len(found):
                ok, why = False, "a sort column is missing"
            else:
                keys = [
                    tuple(_sort_key(normalize_value(r[i], kinds[i], oracle)) for i in idx)
                    for r in actual.rows
                ]
                ok = keys == sorted(keys, reverse=prop.direction == "desc")  # type: ignore[union-attr]
                why = f"rows are not sorted by {', '.join(prop.columns)} {prop.direction}"  # type: ignore[union-attr]
        elif kind == "sum_equals":
            i = col(prop.column)  # type: ignore[union-attr]
            try:
                total = (
                    sum(float(r[i]) for r in actual.rows if r[i] is not None) if i is not None else math.nan
                )
            except (TypeError, ValueError):
                total = math.nan
            ok = not math.isnan(total) and abs(total - prop.value) <= prop.tolerance  # type: ignore[union-attr]
            why = "sum check failed"
        elif kind == "assertion":
            outcome = next(assertion_iter, None)
            ok = bool(outcome) and outcome.get("error") is None and int(outcome.get("rows", 1)) == 0
            why = (
                "a data assertion returned rows"
                if outcome and not outcome.get("error")
                else f"a data assertion failed to run: {(outcome or {}).get('error')}"
            )
        results.append((prop, ok, why))
    return results


def _sort_key(value: Any) -> tuple:
    if value is None:
        return (1, "")
    return (0, value)


# ------------------------------------------------------------------------------------ the verdict
@dataclass
class SqlOutcome:
    verdict: Verdict
    feedback: str
    preview: dict[str, Any] | None
    signature_basis: str


def _error_feedback(error: dict[str, Any], query: str) -> tuple[FailureClass, str]:
    kind = error.get("kind", "runtime")
    message = str(error.get("message", ""))[:600]
    if kind == "timeout" or error.get("sqlstate") == "57014":
        return FailureClass.sql_timeout, f"the query ran past the time limit: {message}"
    if kind == "guard":
        return FailureClass.sql_too_expensive, f"the query plan is too expensive: {message}"
    position = error.get("position")
    where = ""
    if isinstance(position, int) and 0 < position <= len(query):
        line = query.count("\n", 0, position - 1) + 1
        snippet = query[max(0, position - 30) : position + 30].replace("\n", " ")
        where = f" (near line {line}: ...{snippet}...)"
    state = f"[{error.get('sqlstate')}] " if error.get("sqlstate") else ""
    return FailureClass.sql_runtime_error, f"the database rejected the query: {state}{message}{where}"


def sql_verdict(
    oracle: SqlOracle, run: dict[str, Any], query: str, expected: ExpectedResult | None
) -> SqlOutcome:
    datasets: dict[str, Any] = run.get("datasets", {}) or {}
    base = datasets.get("base") or {}
    cand = base.get("candidate") or {}
    guard = base.get("explain") or {}
    preview = None
    if cand and not cand.get("error"):
        preview = {
            "columns": cand.get("columns", []),
            "rows": (cand.get("rows") or [])[:20],
            "truncated": bool(cand.get("truncated")),
            "row_count": cand.get("row_count"),
        }

    def fail(cls: FailureClass, message: str, **extra: Any) -> SqlOutcome:
        verdict = Verdict(passed=False, failure_class=cls, message=message, sql=extra or None)
        return SqlOutcome(verdict, message, preview, f"{cls.value}|{message[:160]}")

    if run.get("error"):
        return fail(
            FailureClass.sql_runtime_error, f"the sandbox could not run the query: {str(run['error'])[:300]}"
        )
    if guard.get("too_expensive"):
        return fail(
            FailureClass.sql_too_expensive,
            f"the query plan is too expensive: {guard.get('summary', '')[:400]}",
        )
    if cand.get("error"):
        cls, message = _error_feedback(cand["error"], query)
        return fail(cls, message)
    actual = ResultSet.from_json(cand)

    level = VerificationLevel.executed
    if oracle.properties:
        results = evaluate_properties(oracle, actual, base.get("assertions", []) or [])
        failed = [(p, why) for p, ok, why in results if not ok]
        if failed:
            visible = [why for p, why in failed if p.visible]
            hidden = len(failed) - len(visible)
            message = "; ".join(visible) if visible else ""
            if hidden:
                message += ("; " if message else "") + f"{hidden} hidden check(s) on the result failed"
            return fail(FailureClass.sql_property_failed, message)
        level = VerificationLevel.properties_passed

    if expected is not None or (oracle.reference_query and base.get("reference")):
        if expected is not None:
            cols, rows, types = expected.columns, expected.rows, None
        else:
            ref = base["reference"]
            if ref.get("error"):
                raise ValueError(f"the reference query failed: {ref['error']}")
            cols, rows, types = ref.get("columns", []), ref.get("rows", []), ref.get("types")
        comparison = compare_results(cols, rows, types, actual, oracle)
        if not comparison.match:
            feedback = comparison.message
            if comparison.extra_examples:
                feedback += "\nExamples of rows your query returned that are not expected:\n" + "\n".join(
                    str(r) for r in comparison.extra_examples
                )
            return fail(
                comparison.failure_class or FailureClass.sql_wrong_rows,
                feedback,
                missing=comparison.missing,
                extra=comparison.extra,
            )
        level = VerificationLevel.rows_matched

        if oracle.reference_query and oracle.differential:
            mutated = [name for name in sorted(datasets) if name != "base"]
            for name in mutated:
                data = datasets[name]
                cand_m, ref_m = data.get("candidate") or {}, data.get("reference") or {}
                if cand_m.get("error"):
                    cls, message = _error_feedback(cand_m["error"], query)
                    return fail(cls, f"on a modified copy of the data: {message}")
                if ref_m.get("error") or not ref_m:
                    continue
                comp = compare_results(
                    ref_m.get("columns", []),
                    ref_m.get("rows", []),
                    ref_m.get("types"),
                    ResultSet.from_json(cand_m),
                    oracle,
                )
                if not comp.match:
                    return fail(
                        FailureClass.sql_differential_failed,
                        "the query matches on the original data but not on a modified copy (rows deleted, "
                        "duplicated or NULLs added): check join types, DISTINCT, NULL handling and "
                        f"hard-coded values. {comp.message}",
                    )
            if mutated:
                level = VerificationLevel.differential_passed

    if level is VerificationLevel.executed:
        message = "the query is safe and runs, but no oracle was given to prove it right"
    else:
        message = f"verified at level {level.value}"
    return SqlOutcome(Verdict(passed=True, level=level, message=message), message, preview, "")


def schema_columns(schema_meta: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    columns: dict[str, dict[str, str]] = {}
    for table in (schema_meta or {}).get("tables", []):
        columns[str(table.get("name", "")).lower()] = {
            str(c.get("name", "")).lower(): str(c.get("type", "")) for c in table.get("columns", [])
        }
    return columns
