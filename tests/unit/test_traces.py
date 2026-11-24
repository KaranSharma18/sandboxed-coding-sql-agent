import orjson

from fixloop_core.models import ExecResult, ExecStatus
from fixloop_core.outputs import FailureClass
from fixloop_core.traces import (
    build_summary,
    compact_trace,
    normalize_message,
    parse_junit_counts,
    parse_report,
)


def report_lines(*records):
    return "\n".join(orjson.dumps(r).decode() for r in records)


FRAMES = [
    {
        "path": "tests/test_dates.py",
        "line": 8,
        "function": "test_parse",
        "source": "assert parse('x') == 3",
        "in_repo": True,
    },
    {"path": "pkg/dates.py", "line": 12, "function": "parse", "source": "return int(text)", "in_repo": True},
    {
        "path": "/usr/lib/python3/site-packages/dateutil/parser.py",
        "line": 99,
        "function": "x",
        "in_repo": False,
    },
]


def sample_report():
    return parse_report(
        report_lines(
            {
                "type": "collection",
                "items": [
                    "tests/test_dates.py::test_parse",
                    "tests/test_dates.py::test_ok",
                    "tests/test_dates.py::test_skip",
                ],
                "errors": [],
            },
            {
                "type": "test",
                "nodeid": "tests/test_dates.py::test_parse",
                "when": "setup",
                "outcome": "passed",
                "duration": 0.01,
            },
            {
                "type": "test",
                "nodeid": "tests/test_dates.py::test_parse",
                "when": "call",
                "outcome": "failed",
                "duration": 0.02,
                "exc_type": "ValueError",
                "message": "invalid literal for int() with base 10: 'x' at 0x7f00aa",
                "frames": FRAMES,
            },
            {"type": "test", "nodeid": "tests/test_dates.py::test_ok", "when": "call", "outcome": "passed"},
            {
                "type": "test",
                "nodeid": "tests/test_dates.py::test_skip",
                "when": "setup",
                "outcome": "skipped",
            },
            "not json at all",
            {"type": "summary", "exitstatus": 1},
        )
    )


def test_parse_report_outcomes():
    report = sample_report()
    assert report.outcome("tests/test_dates.py::test_parse") == "failed"
    assert report.outcome("tests/test_dates.py::test_ok") == "passed"
    assert report.outcome("tests/test_dates.py::test_skip") == "skipped"
    counts = report.counts()
    assert (counts.collected, counts.passed, counts.failed, counts.skipped) == (3, 1, 1, 1)


def test_summary_classifies_and_signs():
    result = ExecResult(exec_id="ex_1", status=ExecStatus.completed, exit_code=1)
    summary = build_summary(result, sample_report(), focus=["tests/test_dates.py::test_parse"])
    assert summary.failure_class is FailureClass.exception_in_code
    other = parse_report(
        report_lines(
            {"type": "collection", "items": ["tests/test_dates.py::test_parse"]},
            {
                "type": "test",
                "nodeid": "tests/test_dates.py::test_parse",
                "when": "call",
                "outcome": "failed",
                "exc_type": "ValueError",
                "message": "invalid literal for int() with base 10: 'x' at 0x7f99bb",
                "frames": FRAMES,
            },
        )
    )
    assert build_summary(result, other).signature == summary.signature  # address differs, same failure


def test_message_normalization():
    assert normalize_message("got 3 at 0xdeadbeef in /tmp/pytest-1/x") == "got <N> at 0xADDR in <TMP>"


def test_compact_trace_collapses_library_frames_and_uses_source():
    result = ExecResult(exec_id="ex_1", status=ExecStatus.completed, exit_code=1)
    summary = build_summary(result, sample_report())
    text = compact_trace(
        summary,
        read_file=lambda p: "a\nb\nc\nd\ne\nf\ng\nh\ni\nj\nk\nl\nm\n" if p == "pkg/dates.py" else None,
    )
    assert "pkg/dates.py:12 in parse" in text
    assert "frame(s) in dateutil" in text
    assert ">    12 | l" in text


def test_timeouts_classified_from_status():
    result = ExecResult(exec_id="ex_2", status=ExecStatus.timeout)
    assert build_summary(result, parse_report("")).failure_class is FailureClass.timeout


def test_junit_counts():
    xml = (
        '<testsuites><testsuite><testcase name="a"/><testcase name="b"><failure/></testcase>'
        '<testcase name="c"><skipped/></testcase><testcase name="d"><error/></testcase></testsuite></testsuites>'
    )
    counts = parse_junit_counts(xml)
    assert (counts.passed, counts.failed, counts.skipped, counts.errors) == (1, 1, 1, 1)
