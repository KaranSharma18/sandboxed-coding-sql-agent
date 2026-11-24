from datetime import date
from pathlib import Path

import orjson
import pytest

from fixloop_core.config import load_config
from fixloop_core.models import ExecResult, ExecStatus, TestCounts, VerificationLevel
from fixloop_core.outputs import FailureClass
from fixloop_core.spec import ExpectedResult, SqlOracle
from fixloop_core.traces import parse_report
from fixloop_core.verify_python import Baseline, changed_line_coverage, compute_verdict, targeted_selection
from fixloop_core.verify_sql import ResultSet, compare_results, schema_columns, sql_verdict, static_check

CONFIG = load_config(Path(__file__).resolve().parents[2] / "config")
T = "tests/test_m.py::test_target"
OK = "tests/test_m.py::test_other"


def report(outcomes, collected=None, coverage=None):
    lines = [{"type": "collection", "items": collected or list(outcomes)}]
    for node, outcome in outcomes.items():
        lines.append({"type": "test", "nodeid": node, "when": "call", "outcome": outcome})
    for node, files in (coverage or {}).items():
        lines.append({"type": "coverage", "nodeid": node, "lines": files})
    return parse_report("\n".join(orjson.dumps(x).decode() for x in lines))


def result(exit_code=0, status=ExecStatus.completed, protected=()):
    return ExecResult(exec_id="ex", status=status, exit_code=exit_code, protected_modified=list(protected))


BASE = Baseline(statuses={T: "failed", OK: "passed"}, collected=[T, OK], strategy="full")


def junit(passed, failed):
    return TestCounts(passed=passed, failed=failed)


def test_passing_candidate():
    verdict = compute_verdict(
        targets=[T],
        baseline=BASE,
        report=report({T: "passed", OK: "passed"}),
        result=result(0),
        junit=junit(2, 0),
        regression="full",
    )
    assert verdict.passed and verdict.level is VerificationLevel.no_regressions


def test_regression_detected():
    verdict = compute_verdict(
        targets=[T],
        baseline=BASE,
        report=report({T: "passed", OK: "failed"}),
        result=result(1),
        junit=junit(1, 1),
        regression="full",
    )
    assert (
        not verdict.passed
        and verdict.failure_class is FailureClass.regression
        and verdict.regressions == [OK]
    )


def test_target_still_failing():
    verdict = compute_verdict(
        targets=[T],
        baseline=BASE,
        report=report({T: "failed", OK: "passed"}),
        result=result(1),
        junit=junit(1, 1),
        regression="full",
    )
    assert verdict.failure_class is FailureClass.targets_failed


def test_skipped_target_is_not_a_pass():
    verdict = compute_verdict(
        targets=[T],
        baseline=BASE,
        report=report({T: "skipped", OK: "passed"}),
        result=result(0),
        junit=None,
        regression="full",
    )
    assert verdict.failure_class is FailureClass.targets_failed


def test_removed_test_and_tampering():
    removed = compute_verdict(
        targets=[T],
        baseline=BASE,
        report=report({T: "passed"}),
        result=result(0),
        junit=junit(1, 0),
        regression="full",
    )
    assert removed.failure_class is FailureClass.tests_removed
    mismatch = compute_verdict(
        targets=[T],
        baseline=BASE,
        report=report({T: "passed", OK: "passed"}),
        result=result(0),
        junit=junit(1, 1),
        regression="full",
    )
    assert mismatch.failure_class is FailureClass.report_mismatch
    exit_lies = compute_verdict(
        targets=[T],
        baseline=BASE,
        report=report({T: "passed", OK: "failed"}),
        result=result(0),
        junit=None,
        regression="full",
    )
    assert exit_lies.failure_class is FailureClass.report_mismatch
    touched = compute_verdict(
        targets=[T],
        baseline=BASE,
        report=report({T: "passed", OK: "passed"}),
        result=result(0, protected=["tests/test_m.py"]),
        junit=None,
        regression="full",
    )
    assert touched.failure_class is FailureClass.test_gaming


def test_parametrized_target_expands():
    cases = {T + "[1]": "passed", T + "[2]": "failed", OK: "passed"}
    base = Baseline(
        statuses={T + "[1]": "failed", T + "[2]": "failed", OK: "passed"},
        collected=list(cases),
        strategy="full",
    )
    verdict = compute_verdict(
        targets=[T], baseline=base, report=report(cases), result=result(1), junit=None, regression="full"
    )
    assert verdict.failure_class is FailureClass.targets_failed and verdict.targets_failed == [T + "[2]"]


def test_changed_line_coverage():
    old = "def f(x):\n    return x\n"
    new = "def f(x):\n    if x:\n        return x + 1\n    return x\n"
    rep = report({T: "passed"}, coverage={T: {"pkg/m.py": [2, 3]}})
    cov = changed_line_coverage({"pkg/m.py": (old, new)}, rep, [T])
    assert cov["changed_executable"] == 2 and cov["executed"] == 2 and cov["share"] == 1.0


def test_targeted_selection_is_seeded_and_bounded():
    collected = [f"tests/test_{i}.py::t" for i in range(50)] + [T]
    chosen, share = targeted_selection(
        collected=collected,
        targets=[T],
        related_files={"tests/test_3.py"},
        durations={},
        budget_s=0.5,
        seed="task-1",
    )
    assert T in chosen and "tests/test_3.py::t" in chosen and share < 1
    again, _ = targeted_selection(
        collected=collected,
        targets=[T],
        related_files={"tests/test_3.py"},
        durations={},
        budget_s=0.5,
        seed="task-1",
    )
    assert chosen == again


# ------------------------------------------------------------------------------------ SQL
SCHEMA = {
    "orders": {"id": "INTEGER", "customer_id": "INTEGER", "amount": "DECIMAL(10,2)", "created_at": "DATE"},
    "customers": {"id": "INTEGER", "region": "VARCHAR"},
}
POLICY = CONFIG.policies.sql


@pytest.mark.parametrize(
    ("query", "cls"),
    [
        ("select * from orders; select 1", FailureClass.sql_policy),
        ("delete from orders", FailureClass.sql_policy),
        ("with x as (select 1 as a) select a from x", FailureClass.hardcoded_result),
        ("select pg_sleep(10) from orders", FailureClass.sql_policy),
        ("select * from read_csv('/etc/passwd')", FailureClass.sql_policy),
        ("select amount from order_lines", FailureClass.sql_unknown_identifier),
        ("select amout from orders", FailureClass.sql_unknown_identifier),
        ("select from where", FailureClass.sql_parse_error),
    ],
)
def test_static_rejections(query, cls):
    dialect = "duckdb" if "read_csv" in query else "postgres"
    result = static_check(query, dialect, SCHEMA, POLICY)
    assert not result.ok and result.failure_class is cls, result.message


def test_static_accepts_good_query_and_hints():
    good = static_check(
        "select c.region, sum(o.amount) as revenue from orders o join customers c on c.id = o.customer_id "
        "group by c.region",
        "duckdb",
        SCHEMA,
        POLICY,
    )
    assert good.ok, good.message
    typo = static_check("select amout from orders", "duckdb", SCHEMA, POLICY)
    assert "amount" in typo.message


def test_time_functions_refused_with_as_of():
    result = static_check(
        "select * from orders where created_at > current_date - 7",
        "duckdb",
        SCHEMA,
        POLICY,
        as_of=date(2026, 1, 1),
    )
    assert not result.ok and "2026-01-01" in result.message


def oracle(**kw):
    return SqlOracle(dialect="duckdb", **kw)


def test_compare_multiset_tolerance_nulls():
    actual = ResultSet(
        columns=["region", "revenue"],
        types=["VARCHAR", "DOUBLE"],
        rows=[["EMEA", 10.0000001], ["APAC", None], ["APAC", None]],
    )
    ok = compare_results(
        ["region", "revenue"], [["APAC", None], ["EMEA", "10"], ["APAC", None]], None, actual, oracle()
    )
    assert ok.match, ok.message
    dup = compare_results(["region", "revenue"], [["APAC", None], ["EMEA", 10]], None, actual, oracle())
    assert not dup.match and dup.extra == 1


def test_compare_columns_and_order():
    actual = ResultSet(columns=["Region", "n"], types=["VARCHAR", "BIGINT"], rows=[["b", 1], ["a", 2]])
    assert compare_results(["region", "n"], [["b", 1], ["a", 2]], None, actual, oracle()).match
    wrong = compare_results(["region", "count"], [["b", 1]], None, actual, oracle())
    assert wrong.failure_class is FailureClass.sql_wrong_columns
    ordered = compare_results(["region", "n"], [["a", 2], ["b", 1]], None, actual, oracle(order_matters=True))
    assert not ordered.match and "order" in ordered.message


def run_payload(candidate, reference=None, mutations=()):
    datasets = {"base": {"candidate": candidate, "reference": reference, "assertions": []}}
    for i, (cand, ref) in enumerate(mutations, start=1):
        datasets[f"mut{i}"] = {"candidate": cand, "reference": ref}
    return {"datasets": datasets}


def rs(rows, cols=("region", "n"), types=("VARCHAR", "BIGINT")):
    return {
        "columns": list(cols),
        "types": list(types),
        "rows": rows,
        "truncated": False,
        "row_count": len(rows),
    }


def test_sql_verdict_levels():
    expected = ExpectedResult(columns=["region", "n"], rows=[["a", 1]])
    outcome = sql_verdict(oracle(expected=expected), run_payload(rs([["a", 1]])), "select ...", expected)
    assert outcome.verdict.passed and outcome.verdict.level is VerificationLevel.rows_matched
    none = sql_verdict(oracle(), run_payload(rs([["a", 1]])), "select ...", None)
    assert none.verdict.passed and none.verdict.level is VerificationLevel.executed
    diff = sql_verdict(
        oracle(reference_query="select ..."),
        run_payload(rs([["a", 1]]), rs([["a", 1]]), mutations=[(rs([["a", 1]]), rs([["a", 2]]))]),
        "select ...",
        None,
    )
    assert not diff.verdict.passed and diff.verdict.failure_class is FailureClass.sql_differential_failed
    good = sql_verdict(
        oracle(reference_query="select ..."),
        run_payload(rs([["a", 1]]), rs([["a", 1]]), mutations=[(rs([["a", 2]]), rs([["a", 2]]))]),
        "select ...",
        None,
    )
    assert good.verdict.level is VerificationLevel.differential_passed


def test_sql_verdict_errors_and_hidden_properties():
    err = sql_verdict(
        oracle(),
        {
            "datasets": {
                "base": {"candidate": {"error": {"sqlstate": "42703", "message": "column x", "position": 8}}}
            }
        },
        "select x from t",
        None,
    )
    assert err.verdict.failure_class is FailureClass.sql_runtime_error and "line 1" in err.feedback
    props = oracle(
        properties=[{"kind": "row_count", "op": "eq", "value": 5}, {"kind": "not_null", "column": "n"}]
    )
    hidden = sql_verdict(props, run_payload(rs([["a", 1]])), "select ...", None)
    assert hidden.verdict.failure_class is FailureClass.sql_property_failed
    assert "hidden" in hidden.feedback and "5" not in hidden.feedback


def test_schema_columns_from_metadata():
    meta = {"tables": [{"name": "Orders", "columns": [{"name": "ID", "type": "INTEGER"}]}]}
    assert schema_columns(meta) == {"orders": {"id": "INTEGER"}}
