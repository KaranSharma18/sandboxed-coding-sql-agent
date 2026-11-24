"""From raw run artifacts to a compact, comparable trace.

Code, not a model, builds the `TraceSummary`: per-test outcomes, the failure records, a failure
class and a normalized *signature* that means "the same failure as before" even when
incidental details (addresses, temp paths, numbers) change. The signature drives no-progress
detection and the failure-memo cache. `compact_trace` turns a summary into at most ~3k tokens
for a repair prompt, deterministically.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import orjson

from fixloop_core.hashing import stable_hash
from fixloop_core.models import ExecResult, ExecStatus, FailureRecord, Frame, TestCounts, TraceSummary
from fixloop_core.outputs import FailureClass

TEST_FILE_RE = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$|conftest\.py$)")
NETWORK_ERRORS = {
    "ConnectionRefusedError",
    "ConnectionError",
    "gaierror",
    "ConnectTimeout",
    "ConnectError",
    "NewConnectionError",
    "MaxRetryError",
    "OperationalError",
}
NETWORK_HINTS = (
    "Name or service not known",
    "Network is unreachable",
    "Temporary failure in name resolution",
    "Connection refused",
    "could not connect to server",
    "nodename nor servname",
)


def is_test_path(path: str) -> bool:
    return bool(TEST_FILE_RE.search(path))


# ------------------------------------------------------------------------------------ parsing
@dataclass
class TestResult:
    __test__ = False

    nodeid: str
    outcome: str = "unknown"  # passed failed error skipped xfailed xpassed
    duration: float = 0.0
    phase: str = "call"
    exc_type: str = ""
    message: str = ""
    assertion: str | None = None
    frames: list[dict[str, Any]] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    longrepr: str = ""


@dataclass
class ParsedReport:
    collected: list[str] = field(default_factory=list)
    collection_errors: list[dict[str, Any]] = field(default_factory=list)
    tests: dict[str, TestResult] = field(default_factory=dict)
    coverage: dict[str, dict[str, list[int]]] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    present: bool = False

    def outcome(self, nodeid: str) -> str:
        result = self.tests.get(nodeid)
        return result.outcome if result else "missing"

    def counts(self) -> TestCounts:
        counts = TestCounts(collected=len(self.collected))
        for result in self.tests.values():
            if result.outcome == "passed":
                counts.passed += 1
            elif result.outcome == "failed":
                counts.failed += 1
            elif result.outcome == "error":
                counts.errors += 1
            elif result.outcome == "skipped":
                counts.skipped += 1
            elif result.outcome == "xfailed":
                counts.xfailed += 1
            elif result.outcome == "xpassed":
                counts.xpassed += 1
        counts.errors += len(self.collection_errors)
        return counts


def parse_report(text: str) -> ParsedReport:
    """Parse the plugin's JSONL report. Unknown or malformed lines are skipped, never trusted."""
    report = ParsedReport()
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            record = orjson.loads(raw)
        except orjson.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        kind = record.get("type")
        report.present = True
        if kind == "collection":
            report.collected = [str(x) for x in record.get("items", [])][:100_000]
            report.collection_errors = [e for e in record.get("errors", []) if isinstance(e, dict)][:200]
        elif kind == "test":
            _merge_phase(report, record)
        elif kind == "coverage":
            nodeid = str(record.get("nodeid", ""))
            lines = record.get("lines", {})
            if isinstance(lines, dict):
                report.coverage[nodeid] = {
                    str(k): [int(n) for n in v][:20_000] for k, v in lines.items() if isinstance(v, list)
                }
        elif kind == "summary":
            report.summary = record
    return report


def _merge_phase(report: ParsedReport, record: dict[str, Any]) -> None:
    nodeid = str(record.get("nodeid", ""))
    when = str(record.get("when", "call"))
    outcome = str(record.get("outcome", ""))
    wasxfail = bool(record.get("wasxfail"))
    result = report.tests.setdefault(nodeid, TestResult(nodeid=nodeid))
    result.duration += float(record.get("duration", 0.0) or 0.0)
    failed_here = outcome == "failed"
    if when in ("setup", "teardown"):
        if failed_here:
            result.outcome, result.phase = "error", when
        elif outcome == "skipped" and when == "setup" and result.outcome == "unknown":
            result.outcome = "xfailed" if wasxfail else "skipped"
    elif when == "call" and result.outcome != "error":
        if outcome == "passed":
            result.outcome = "xpassed" if wasxfail else "passed"
        elif outcome == "skipped":
            result.outcome = "xfailed" if wasxfail else "skipped"
        elif failed_here:
            result.outcome = "failed"
        result.phase = "call"
    if failed_here or (wasxfail and outcome == "skipped"):
        result.exc_type = str(record.get("exc_type") or result.exc_type or "")
        result.message = str(record.get("message") or result.message or "")[:2000]
        result.assertion = record.get("assertion") or result.assertion
        frames = record.get("frames")
        if isinstance(frames, list) and frames:
            result.frames = [f for f in frames if isinstance(f, dict)][-40:]
        result.longrepr = str(record.get("longrepr") or "")[:6000]
    if record.get("stdout"):
        result.stdout = str(record["stdout"])[-4000:]
    if record.get("stderr"):
        result.stderr = str(record["stderr"])[-4000:]


def parse_junit_counts(text: str) -> TestCounts | None:
    """Counts from pytest's own JUnit XML; used only to cross-check the plugin's report."""
    if not text.strip():
        return None
    try:
        root = ET.fromstring(text)  # noqa: S314 - local, size-capped artifact
    except ET.ParseError:
        return None
    counts = TestCounts()
    for case in root.iter("testcase"):
        tags = {child.tag for child in case}
        if "error" in tags:
            counts.errors += 1
        elif "failure" in tags:
            counts.failed += 1
        elif "skipped" in tags:
            counts.skipped += 1
        else:
            counts.passed += 1
    return counts


# ------------------------------------------------------------------------------------ signatures
_NORMALIZERS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"0x[0-9a-fA-F]+"), "0xADDR"),
    (re.compile(r"(/private)?/(tmp|var/folders|var/tmp)/[^\s'\"):]+"), "<TMP>"),
    (re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"), "<UUID>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:?\d{2}|Z)?"), "<TS>"),
    (re.compile(r"\b\d+(\.\d+)?\b"), "<N>"),
    (re.compile(r"\s+"), " "),
]


def normalize_message(message: str) -> str:
    text = message.strip().split("\n", 1)[0]
    for pattern, replacement in _NORMALIZERS:
        text = pattern.sub(replacement, text)
    return text[:200]


def top_repo_frame(frames: list[Frame]) -> Frame | None:
    for frame in reversed(frames):
        if frame.in_repo:
            return frame
    return None


def signature_of(failures: list[FailureRecord], status: ExecStatus = ExecStatus.completed) -> str:
    """Normalized identity of a set of failures; equal signatures mean 'the same failure again'."""
    items = []
    for failure in failures:
        frame = top_repo_frame(failure.frames)
        items.append(
            (
                failure.test_id,
                failure.exc_type,
                normalize_message(failure.message),
                f"{frame.path}:{frame.function}" if frame else "",
            )
        )
    if not items and status is ExecStatus.completed:
        return ""
    return stable_hash("sig-v1", status.value, sorted(items))[:24]


# ------------------------------------------------------------------------------------ classification
def classify(failures: list[FailureRecord], status: ExecStatus) -> FailureClass:
    if status is ExecStatus.timeout:
        return FailureClass.timeout
    if status is ExecStatus.oom:
        return FailureClass.oom
    if not failures:
        return FailureClass.unknown
    first = failures[0]
    exc = first.exc_type.rsplit(".", 1)[-1]
    if first.phase == "collect":
        if exc in ("SyntaxError", "IndentationError", "TabError"):
            return FailureClass.syntax_error
        if exc in ("ImportError", "ModuleNotFoundError"):
            return FailureClass.import_error
        return FailureClass.collection_error
    if exc in ("SyntaxError", "IndentationError", "TabError"):
        return FailureClass.syntax_error
    if exc in ("ImportError", "ModuleNotFoundError"):
        return FailureClass.import_error
    if exc == "RecursionError":
        return FailureClass.recursion
    if exc == "FixloopTestTimeout":
        return FailureClass.timeout
    if exc in NETWORK_ERRORS or any(hint in first.message for hint in NETWORK_HINTS):
        return FailureClass.env_error
    frame = top_repo_frame(first.frames)
    if exc == "AssertionError" and (frame is None or is_test_path(frame.path)):
        return FailureClass.assertion_mismatch
    if frame is not None and not is_test_path(frame.path):
        return FailureClass.exception_in_code
    if frame is not None:
        return FailureClass.exception_in_test
    return FailureClass.env_error


# ------------------------------------------------------------------------------------ building a summary
def _frames(raw: list[dict[str, Any]]) -> list[Frame]:
    frames = []
    for item in raw:
        try:
            frames.append(
                Frame(
                    path=str(item.get("path", "")),
                    line=int(item.get("line", 0) or 0),
                    function=str(item.get("function", "")),
                    source=str(item.get("source", ""))[:300],
                    in_repo=bool(item.get("in_repo")),
                )
            )
        except (TypeError, ValueError):
            continue
    return frames


def failure_records(report: ParsedReport, focus: list[str] | None = None) -> list[FailureRecord]:
    """Failure records ordered by importance: collection errors, focus (target) tests, then the rest."""
    records: list[FailureRecord] = []
    for error in report.collection_errors:
        records.append(
            FailureRecord(
                test_id=str(error.get("nodeid", "<collection>")),
                phase="collect",
                exc_type=str(error.get("exc_type", "")),
                message=str(error.get("message", ""))[:500],
                frames=_frames(error.get("frames", []) or []),
                stderr_tail=str(error.get("longrepr", ""))[-1000:],
            )
        )
    focus_set = set(focus or [])
    failing = [r for r in report.tests.values() if r.outcome in ("failed", "error")]
    failing.sort(key=lambda r: (r.nodeid not in focus_set, r.nodeid))
    for result in failing:
        records.append(
            FailureRecord(
                test_id=result.nodeid,
                phase=result.phase if result.phase in ("setup", "call", "teardown") else "call",
                exc_type=result.exc_type,
                message=result.message[:500],
                assertion_diff=(result.assertion or None) and str(result.assertion)[:1000],
                frames=_frames(result.frames),
                stdout_tail=result.stdout[-1000:],
                stderr_tail=result.stderr[-1000:],
            )
        )
    return records


def build_summary(
    result: ExecResult, report: ParsedReport, focus: list[str] | None = None, only: list[str] | None = None
) -> TraceSummary:
    """Summarize one execution. `only` restricts the failures considered (for example to the
    targets plus regressions) so pre-existing failures elsewhere do not pollute the signature."""
    failures = failure_records(report, focus)
    if only is not None:
        allowed = set(only)
        failures = [f for f in failures if f.phase == "collect" or f.test_id in allowed]
    notes: list[str] = []
    if result.status is not ExecStatus.completed:
        notes.append(f"execution ended with status {result.status.value}")
    if not report.present and result.status is ExecStatus.completed:
        notes.append("the test run produced no report (the process may have crashed early)")
    if result.error:
        notes.append(result.error[:500])
    return TraceSummary(
        exec_id=result.exec_id,
        status=result.status,
        counts=report.counts(),
        failures=failures[:20],
        failure_class=classify(failures, result.status),
        signature=signature_of(failures, result.status),
        duration_ms=result.duration_ms,
        notes=notes,
    )


# ------------------------------------------------------------------------------------ compaction
def _window(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.65)
    return text[:head] + "\n... [cut] ...\n" + text[-(limit - head) :]


def compact_trace(
    summary: TraceSummary,
    read_file: Callable[[str], str | None] | None = None,
    budget_tokens: int = 3000,
    raw_stdout: str = "",
    raw_stderr: str = "",
) -> str:
    """Deterministic compaction of a trace for a prompt (about `budget_tokens` tokens)."""
    budget_chars = int(budget_tokens * 3.5)
    c = summary.counts
    lines = [
        f"Execution status: {summary.status.value}. Collected {c.collected}; passed {c.passed}, "
        f"failed {c.failed}, errors {c.errors}, skipped {c.skipped}. Failure class: "
        f"{summary.failure_class.value}."
    ]
    lines += [f"Note: {note}" for note in summary.notes]
    groups: dict[tuple[str, str, str], list[FailureRecord]] = {}
    for failure in summary.failures:
        frame = top_repo_frame(failure.frames)
        key = (
            failure.exc_type,
            normalize_message(failure.message),
            f"{frame.path}:{frame.function}" if frame else "",
        )
        groups.setdefault(key, []).append(failure)
    for members in groups.values():
        first = members[0]
        lines.append("")
        lines.append(f"FAILED {first.test_id} ({first.phase}): {first.exc_type}: {first.message}".rstrip())
        if len(members) > 1:
            base = first.test_id.split("[", 1)[0]
            lines.append(
                f"  ({len(members) - 1} more failing the same way, e.g. "
                f"{', '.join(m.test_id for m in members[1:4])}{' ...' if len(members) > 4 else ''}; "
                f"group {base})"
            )
        if first.assertion_diff:
            lines.append("  assertion: " + _window(first.assertion_diff, 700).replace("\n", "\n    "))
        lines.extend(_render_frames(first.frames, read_file))
        if first.stdout_tail.strip():
            lines.append(
                "  captured stdout (tail):\n    " + _window(first.stdout_tail, 500).replace("\n", "\n    ")
            )
        if first.stderr_tail.strip():
            lines.append(
                "  captured stderr (tail):\n    " + _window(first.stderr_tail, 500).replace("\n", "\n    ")
            )
    if not summary.failures and (raw_stdout or raw_stderr):
        lines.append("Process output (tail):")
        lines.append(_window((raw_stdout + "\n" + raw_stderr).strip(), 1500))
    text = "\n".join(lines)
    return _window(text, budget_chars)


def _render_frames(frames: list[Frame], read_file: Callable[[str], str | None] | None) -> list[str]:
    out: list[str] = []
    library_run: list[Frame] = []

    def flush() -> None:
        if library_run:
            packages = sorted({_package_of(f.path) for f in library_run})
            out.append(f"  ... {len(library_run)} frame(s) in {', '.join(packages)} ...")
            library_run.clear()

    for frame in frames[-12:]:
        if not frame.in_repo:
            library_run.append(frame)
            continue
        flush()
        out.append(f"  {frame.path}:{frame.line} in {frame.function}")
        context = _source_context(read_file, frame.path, frame.line) if read_file else None
        if context:
            out.extend("    " + line for line in context)
        elif frame.source:
            out.append(f"    > {frame.source.strip()}")
    flush()
    return out


def _package_of(path: str) -> str:
    match = re.search(r"(?:site|dist)-packages/([^/]+)", path)
    if match:
        return match.group(1)
    return path.rsplit("/", 1)[-1] if "/" in path else path or "?"


def _source_context(
    read_file: Callable[[str], str | None], path: str, line: int, radius: int = 3
) -> list[str] | None:
    text = read_file(path)
    if not text or line <= 0:
        return None
    source = text.splitlines()
    lo, hi = max(1, line - radius), min(len(source), line + radius)
    return [f"{'>' if n == line else ' '} {n:5d} | {source[n - 1]}" for n in range(lo, hi + 1)]
