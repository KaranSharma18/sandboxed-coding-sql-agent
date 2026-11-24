"""Progress scores: how to rank attempts, so the loop can always repair from the best state.

A score is a tuple compared left to right; each later field only breaks ties. The untouched
baseline is scored too, so when every attempt made things worse the best result is "no change".
"""

from __future__ import annotations

from fixloop_core.models import Verdict
from fixloop_core.outputs import FailureClass
from fixloop_core.traces import ParsedReport
from fixloop_core.verify_python import PASSING, expand_targets

NOT_RUNNABLE = {FailureClass.syntax_error, FailureClass.import_error, FailureClass.collection_error}
NEVER_EXECUTED = {
    FailureClass.patch_apply_error,
    FailureClass.policy_violation,
    FailureClass.patch_too_large,
    FailureClass.format_error,
    FailureClass.provider_refusal,
    FailureClass.test_gaming,
}


def python_score(
    verdict: Verdict | None, report: ParsedReport | None, targets: list[str], changed_lines: int
) -> list[float]:
    """(runnable, targets passing, -regressions, -collection errors, -changed lines)."""
    if verdict is None or report is None or (verdict.failure_class in NEVER_EXECUTED):
        return [-1.0, 0.0, 0.0, 0.0, float(-changed_lines)]
    if verdict.passed:
        passing = sum(len(nodes) for nodes in expand_targets(targets, report.collected).values())
        return [1.0, float(passing), 0.0, 0.0, float(-changed_lines)]
    runnable = 0.0 if verdict.failure_class in NOT_RUNNABLE else 1.0
    passing = sum(
        1
        for nodes in expand_targets(targets, report.collected).values()
        for n in nodes
        if report.outcome(n) in PASSING
    )
    return [
        runnable,
        float(passing),
        float(-len(verdict.regressions)),
        float(-len(report.collection_errors)),
        float(-changed_lines),
    ]


SQL_PARSE = {
    FailureClass.sql_parse_error,
    FailureClass.sql_policy,
    FailureClass.sql_unknown_identifier,
    FailureClass.hardcoded_result,
    FailureClass.format_error,
    FailureClass.provider_refusal,
}
SQL_EXEC = {FailureClass.sql_runtime_error, FailureClass.sql_timeout, FailureClass.sql_too_expensive}


def sql_score(verdict: Verdict | None, query: str) -> list[float]:
    """(parses, executes, right columns, share of matching rows, properties passed, -query length)."""
    if verdict is None:
        return [0.0, 0.0, 0.0, 0.0, 0.0, float(-len(query))]
    if verdict.passed:
        return [1.0, 1.0, 1.0, 1.0, 1.0, float(-len(query))]
    cls = verdict.failure_class
    parses = 0.0 if cls in SQL_PARSE else 1.0
    executes = 0.0 if (cls in SQL_PARSE or cls in SQL_EXEC) else 1.0
    columns = 0.0 if (executes == 0.0 or cls == FailureClass.sql_wrong_columns) else 1.0
    share = 0.0
    if cls == FailureClass.sql_wrong_rows and verdict.sql:
        missing, extra = verdict.sql.get("missing", 0), verdict.sql.get("extra", 0)
        share = max(0.0, 1.0 - (missing + extra) / max(1, (missing + extra) * 2))
    elif cls in (FailureClass.sql_property_failed, FailureClass.sql_differential_failed):
        share = 1.0 if cls == FailureClass.sql_differential_failed else 0.5
    properties = 1.0 if cls == FailureClass.sql_differential_failed else 0.0
    return [parses, executes, columns, share, properties, float(-len(query))]


def better(a: list[float], b: list[float] | None) -> bool:
    """Is score `a` strictly better than `b`? Ties keep the earlier attempt."""
    return b is None or a > b
