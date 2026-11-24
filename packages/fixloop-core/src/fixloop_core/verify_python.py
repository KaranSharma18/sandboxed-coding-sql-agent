"""Python verification: turning run artifacts into a verdict (design section 10).

No model takes part. The checks run cheapest first and stop at the first hard failure:
collection, targets, regressions against the baseline, anti-gaming. Hidden tests, stability
reruns and changed-line coverage are separate steps the driver runs only when these pass.
"""

from __future__ import annotations

import ast
import difflib
import random
from dataclasses import dataclass, field
from typing import Any

from fixloop_core.models import ExecResult, ExecStatus, TestCounts, Verdict, VerificationLevel
from fixloop_core.outputs import FailureClass
from fixloop_core.traces import ParsedReport, is_test_path

PASSING = {"passed", "xpassed"}


@dataclass
class Baseline:
    statuses: dict[str, str]
    collected: list[str]
    strategy: str  # full | targeted | none
    selection: list[str] = field(default_factory=list)  # node ids/files run for regressions ([] = all)
    share: float | None = None
    duration_s: float = 0.0
    collection_errors: list[str] = field(default_factory=list)  # files that already failed to collect

    @property
    def passing(self) -> set[str]:
        return {node for node, outcome in self.statuses.items() if outcome in PASSING}


def covers(target: str, nodeid: str) -> bool:
    """Does a target id select this collected node? (a file, a class, or a test with/without params)"""
    if nodeid == target:
        return True
    if "::" not in target:
        return nodeid.startswith(target + "::")
    return nodeid.startswith((target + "[", target + "::"))


def expand_targets(targets: list[str], collected: list[str]) -> dict[str, list[str]]:
    return {target: [n for n in collected if covers(target, n)] for target in targets}


def targets_outcome(report: ParsedReport, targets: list[str]) -> tuple[bool, list[str]]:
    """(all targets pass, failing target node ids). A target that matches nothing counts as failing."""
    failing: list[str] = []
    expanded = expand_targets(targets, report.collected or list(report.tests))
    for target, nodes in expanded.items():
        if not nodes:
            failing.append(target)
            continue
        failing += [n for n in nodes if report.outcome(n) not in PASSING]
    return not failing, failing


def baseline_from_report(
    report: ParsedReport, strategy: str, selection: list[str], share: float | None, duration_s: float
) -> Baseline:
    statuses = {node: result.outcome for node, result in report.tests.items()}
    errors = sorted({str(e.get("nodeid", "")) for e in report.collection_errors})
    return Baseline(
        statuses=statuses,
        collected=list(report.collected),
        strategy=strategy,
        selection=selection,
        share=share,
        duration_s=duration_s,
        collection_errors=errors,
    )


def check_report_consistency(
    report: ParsedReport, junit: TestCounts | None, exit_code: int | None
) -> str | None:
    """Two independent reports and the exit code must agree, or the run counts as tampered with."""
    counts = report.counts()
    failing = counts.failed + counts.errors
    if junit is not None:
        junit_failing = junit.failed + junit.errors
        if junit.passed != counts.passed + counts.xpassed or junit_failing != failing:
            return (
                f"the plugin report ({counts.passed} passed, {failing} failing) and JUnit "
                f"({junit.passed} passed, {junit_failing} failing) disagree"
            )
    if exit_code == 0 and failing:
        return "pytest exited 0 although the report shows failures"
    if exit_code == 1 and not failing:
        return "pytest exited 1 although the report shows no failures"
    return None


def compute_verdict(
    *,
    targets: list[str],
    baseline: Baseline,
    report: ParsedReport,
    result: ExecResult,
    junit: TestCounts | None,
    regression: str,
) -> Verdict:
    counts = report.counts()

    def fail(cls: FailureClass, message: str, **extra: Any) -> Verdict:
        return Verdict(
            passed=False,
            failure_class=cls,
            message=message,
            counts=counts,
            regression_strategy=baseline.strategy,
            regression_share=baseline.share,
            **extra,
        )  # type: ignore[arg-type]

    if result.status is ExecStatus.timeout:
        return fail(FailureClass.timeout, "the test run hit its time limit")
    if result.status is ExecStatus.oom:
        return fail(FailureClass.oom, "the test run ran out of memory")
    if result.status is not ExecStatus.completed:
        return fail(FailureClass.unknown, f"the test run ended with status {result.status.value}")
    if result.protected_modified:
        return fail(
            FailureClass.test_gaming,
            "protected files changed during the run: " + ", ".join(result.protected_modified[:5]),
        )
    if not report.present:
        return fail(
            FailureClass.collection_error, "pytest produced no report (it may have crashed during start-up)"
        )
    # A file that already failed to collect before the change is a pre-existing problem, unless it
    # holds a target (then it must be fixed). Only new collection errors fail the candidate.
    target_files = {t.split("::", 1)[0] for t in targets}
    new_errors = [
        e
        for e in report.collection_errors
        if str(e.get("nodeid", "")) not in baseline.collection_errors
        or str(e.get("nodeid", "")).split("::", 1)[0] in target_files
    ]
    if new_errors:
        first = new_errors[0]
        return fail(
            FailureClass.collection_error,
            f"collection failed in {first.get('nodeid')}: {first.get('exc_type')}: "
            f"{str(first.get('message'))[:200]}",
        )
    mismatch = check_report_consistency(report, junit, result.exit_code)
    if mismatch:
        return fail(FailureClass.report_mismatch, mismatch)
    removed = sorted(set(baseline.collected) - set(report.collected))
    if removed:
        return fail(
            FailureClass.tests_removed,
            f"{len(removed)} test(s) collected at baseline are gone: " + ", ".join(removed[:5]),
            regressions=removed[:50],
        )
    ok, failing_targets = targets_outcome(report, targets)
    regressions = sorted(n for n in baseline.passing if report.outcome(n) not in PASSING)
    preexisting = sorted(
        n
        for n, o in baseline.statuses.items()
        if o not in PASSING and report.outcome(n) not in PASSING and not any(covers(t, n) for t in targets)
    )
    if not ok:
        passed_targets = sum(
            1
            for nodes in expand_targets(targets, report.collected).values()
            for n in nodes
            if report.outcome(n) in PASSING
        )
        return fail(
            FailureClass.targets_failed,
            f"{len(failing_targets)} target test(s) still fail ({passed_targets} pass)"
            + (f"; {len(regressions)} regression(s)" if regressions else ""),
            targets_failed=failing_targets[:50],
            regressions=regressions[:50],
            preexisting_failures=preexisting[:50],
        )
    if regressions and regression != "none":
        return fail(
            FailureClass.regression,
            f"targets pass but {len(regressions)} test(s) that passed before now fail: "
            + ", ".join(regressions[:5]),
            regressions=regressions[:50],
            preexisting_failures=preexisting[:50],
        )
    level = VerificationLevel.no_regressions if regression != "none" else VerificationLevel.targets_passed
    warnings = []
    if baseline.strategy == "targeted":
        share = f"{baseline.share:.0%}" if baseline.share is not None else "part"
        warnings.append(f"regression check was targeted ({share} of the suite), not the full suite")
    if preexisting:
        warnings.append(f"{len(preexisting)} test(s) were already failing before the change and still fail")
    return Verdict(
        passed=True,
        level=level,
        message="all target tests pass with no regressions"
        if level is VerificationLevel.no_regressions
        else "all target tests pass",
        counts=counts,
        preexisting_failures=preexisting[:50],
        warnings=warnings,
        regression_strategy=baseline.strategy,
        regression_share=baseline.share,
    )


def hidden_verdict(report: ParsedReport, result: ExecResult) -> tuple[bool, int, str]:
    """Hidden tests must all pass. Only the *count* of failures ever goes back to the model."""
    if result.status is not ExecStatus.completed or not report.present:
        return False, max(1, len(report.tests)), "the hidden-test run did not complete"
    counts = report.counts()
    failing = counts.failed + counts.errors
    if counts.collected == 0 and not report.tests:
        return False, 1, "no hidden tests were collected"
    return failing == 0, failing, f"{failing} hidden check(s) failed"


def stable_across(reports: list[tuple[ParsedReport, ExecResult]], targets: list[str]) -> tuple[bool, str]:
    for index, (report, result) in enumerate(reports, start=1):
        if result.status is not ExecStatus.completed:
            return False, f"rerun {index} ended with status {result.status.value}"
        ok, failing = targets_outcome(report, targets)
        if not ok:
            return False, f"rerun {index} (shuffled order) failed: {', '.join(failing[:3])}"
    return True, "stable across reruns"


# ------------------------------------------------------------------------------------ coverage of changes
def executable_lines(text: str) -> set[int]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt) and not isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)
        ):
            lines.add(node.lineno)
    return lines


def changed_line_numbers(old: str, new: str) -> set[int]:
    changed: set[int] = set()
    matcher = difflib.SequenceMatcher(None, old.splitlines(), new.splitlines(), autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "insert"):
            changed.update(range(j1 + 1, j2 + 1))
    return changed


def changed_line_coverage(
    changes: dict[str, tuple[str, str]], report: ParsedReport, targets: list[str]
) -> dict[str, object]:
    """Share of changed executable lines that ran while the target tests ran."""
    executed: dict[str, set[int]] = {}
    for nodeid, files in report.coverage.items():
        if any(covers(t, nodeid) for t in targets):
            for path, lines in files.items():
                executed.setdefault(path, set()).update(lines)
    total = hit = 0
    per_file: dict[str, dict[str, int]] = {}
    for path, (old, new) in changes.items():
        if not path.endswith(".py") or is_test_path(path):
            continue
        relevant = changed_line_numbers(old, new) & executable_lines(new)
        covered = relevant & executed.get(path, set())
        total += len(relevant)
        hit += len(covered)
        per_file[path] = {"changed_executable": len(relevant), "executed": len(covered)}
    share = (hit / total) if total else None
    return {"share": share, "changed_executable": total, "executed": hit, "files": per_file}


# ------------------------------------------------------------------------------------ regression selection
def targeted_selection(
    *,
    collected: list[str],
    targets: list[str],
    related_files: set[str],
    durations: dict[str, float],
    budget_s: float,
    seed: str,
) -> tuple[list[str], float]:
    """Tests in the target modules, tests in modules related to the change, then a seeded sample."""
    target_files = {t.split("::", 1)[0] for t in targets}
    chosen: list[str] = []
    seen: set[str] = set()
    spent = 0.0

    def take(node: str) -> None:
        nonlocal spent
        if node not in seen:
            seen.add(node)
            chosen.append(node)
            spent += durations.get(node, 0.05)

    for node in collected:
        if node.split("::", 1)[0] in target_files or any(covers(t, node) for t in targets):
            take(node)
    for node in collected:
        if node.split("::", 1)[0] in related_files:
            take(node)
    rest = [n for n in collected if n not in seen]
    random.Random(seed).shuffle(rest)
    for node in rest:
        if spent >= budget_s:
            break
        take(node)
    share = len(chosen) / len(collected) if collected else 1.0
    return sorted(chosen), share
