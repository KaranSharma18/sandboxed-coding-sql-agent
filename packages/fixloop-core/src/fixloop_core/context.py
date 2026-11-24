"""Deterministic context assembly (design section 6).

The same snapshot, trace and attempt history always produce byte-identical prompts. That
matters twice: the L1 cache keys on the exact prompt, and Gemini's implicit caching only
discounts a prefix that is identical across calls.

Prompt layout, stable first:
  system  shared rules for the task family
  1 task frame          never changes within a task
  2 repository map      never changes within a task
  3 target tests        never changes within a task
  4 first file selection (original versions)  never changes within a task
  5 files added later   append-only
  6 attempt history     every attempt
  7 current versions of files changed by the best attempt
  8 the current failure
  9 the role's instruction (last, so plan/patch/repair share blocks 1-4 word for word)
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterable

from fixloop_core.models import AttemptRecord, ManifestEntry, TraceSummary
from fixloop_core.repomap import outline_python
from fixloop_core.secrets import is_secret_file, redact_text
from fixloop_core.spec import PythonOracle, SqlOracle, TaskSpec
from fixloop_core.traces import is_test_path, top_repo_frame

FILE_BLOCK_TOKENS = 20_000  # first selection, shared by every role of the task
WINDOW_RADIUS = 40


def estimate_tokens(text: str) -> int:
    """Local estimate (characters / 3.5, plus 15%). Used for budgets; the bill uses real counts."""
    return int(len(text) / 3.5 * 1.15) + 1


def untrusted(kind: str, text: str, path: str | None = None) -> str:
    safe = text.replace("</untrusted", "</untrusted_")
    attr = f' path="{path}"' if path else ""
    return f'<untrusted kind="{kind}"{attr}>\n{safe}\n</untrusted>'


def safe_text(path: str, text: str) -> str:
    """Redact secrets before anything reaches a prompt."""
    redacted, _ = redact_text(text)
    return redacted


# ------------------------------------------------------------------------------------ ranking
_IDENT_FROM_ERROR = re.compile(r"'([A-Za-z_][A-Za-z0-9_]{2,})'|\b([A-Za-z_][A-Za-z0-9_]*\([^)]{0,40}\))")
_PATHLIKE = re.compile(r"\b([\w./-]+\.(?:py|cfg|toml|ini|json|yaml|yml|txt|sql))\b")


def rank_files(
    *,
    paths: list[str],
    manifest: dict[str, ManifestEntry],
    read: Callable[[str], str | None],
    target_tests: list[str],
    summary: TraceSummary | None,
    description: str,
    graph: dict[str, frozenset[str]],
    max_file_bytes: int,
    requested: Iterable[str] = (),
) -> list[tuple[str, str]]:
    """Candidate files in rank order, each with the reason it was picked."""
    ranked: list[tuple[str, str]] = []
    seen: set[str] = set()
    available = set(paths)

    def add(path: str, reason: str) -> None:
        if path in seen or path not in available:
            return
        entry = manifest.get(path)
        if entry is not None and (entry.binary or entry.size > max_file_bytes):
            return
        if is_secret_file(path):
            return
        seen.add(path)
        ranked.append((path, reason))

    if summary:
        for failure in summary.failures:
            for frame in reversed(failure.frames):
                if frame.in_repo and not is_test_path(frame.path):
                    add(frame.path, "in the failing traceback")
    target_files = sorted({node.split("::", 1)[0] for node in target_tests})
    for test_file in target_files:
        parts = test_file.split("/")
        for depth in range(len(parts)):
            add("/".join([*parts[:depth], "conftest.py"]), "fixtures for the target tests")
    for test_file in target_files:
        for dep in sorted(graph.get(test_file, frozenset())):
            add(dep, "imported by a target test")
    if summary:
        idents: set[str] = set()
        for failure in summary.failures:
            for match in _IDENT_FROM_ERROR.finditer(failure.message):
                ident = match.group(1) or (match.group(2) or "").split("(", 1)[0]
                if ident and not ident.startswith("test"):
                    idents.add(ident)
        if idents:
            pattern = re.compile(
                r"^\s*(?:def|class)\s+(" + "|".join(map(re.escape, sorted(idents))) + r")\b", re.M
            )
            for path in paths:
                if path.endswith(".py") and not is_test_path(path):
                    text = read(path)
                    if text and pattern.search(text):
                        add(path, "defines a name from the error")
    for match in _PATHLIKE.finditer(description or ""):
        add(match.group(1), "named in the description")
    for path in requested:
        add(path, "requested by the plan")
    return ranked


# ------------------------------------------------------------------------------------ rendering files
def _numbered_windows(text: str, focus: set[int], radius: int = WINDOW_RADIUS) -> str:
    lines = text.splitlines()
    keep: set[int] = set()
    for line in focus:
        keep.update(range(max(1, line - radius), min(len(lines), line + radius) + 1))
    out, previous = [], 0
    for n in sorted(keep):
        if n != previous + 1:
            out.append(f"... (lines {previous + 1}-{n - 1} omitted) ...")
        out.append(lines[n - 1])
        previous = n
    if previous < len(lines):
        out.append(f"... (lines {previous + 1}-{len(lines)} omitted) ...")
    return "\n".join(out)


def render_file(path: str, text: str, budget_chars: int, focus: set[int]) -> tuple[str, bool]:
    """Whole file if it fits; otherwise an outline plus windows around the lines that matter."""
    text = safe_text(path, text)
    if len(text) <= budget_chars:
        return text, True
    outline = "\n".join(outline_python(text)) if path.endswith(".py") else ""
    windows = _numbered_windows(text, focus or {1})
    shown = f"[outline]\n{outline}\n[excerpts]\n{windows}" if outline else windows
    return shown[:budget_chars], False


def file_section(path: str, rendered: str, full: bool, reason: str, sha: str = "") -> str:
    header = f"FILE {path}" + (f" (sha256 {sha[:12]})" if sha else "") + ("" if full else " [excerpt]")
    return f"{header} - {reason}\n" + untrusted("file", rendered, path)


def pack_files(
    ranked: list[tuple[str, str]],
    read: Callable[[str], str | None],
    budget_tokens: int,
    focus: dict[str, set[int]],
    manifest: dict[str, ManifestEntry] | None = None,
    exclude: set[str] | None = None,
) -> tuple[str, list[str]]:
    """Add whole files in rank order until the budget is used; shrink the rest to excerpts."""
    budget_chars = int(budget_tokens * 3.5 / 1.15)
    sections: list[str] = []
    used, included = 0, []
    for path, reason in ranked:
        if exclude and path in exclude:
            continue
        text = read(path)
        if text is None:
            continue
        remaining = budget_chars - used
        if remaining < 800:
            break
        rendered, full = render_file(path, text, min(remaining, 60_000), focus.get(path, set()))
        sha = manifest[path].sha256 if manifest and path in manifest else ""
        section = file_section(path, rendered, full, reason, sha)
        sections.append(section)
        used += len(section)
        included.append(path)
    return "\n\n".join(sections), included


def focus_lines(summary: TraceSummary | None) -> dict[str, set[int]]:
    focus: dict[str, set[int]] = {}
    if summary:
        for failure in summary.failures:
            for frame in failure.frames:
                if frame.in_repo and frame.line > 0:
                    focus.setdefault(frame.path, set()).add(frame.line)
    return focus


# ------------------------------------------------------------------------------------ python blocks
def task_frame_python(spec: TaskSpec, protected: list[str]) -> str:
    oracle = spec.oracle
    assert isinstance(oracle, PythonOracle)
    goal = (
        "Make the failing target tests pass without breaking tests that pass today."
        if spec.kind == "python.fix"
        else "Implement the behaviour the provided tests describe; they are frozen and judge the result."
    )
    lines = [
        f'<task kind="{spec.kind}">',
        goal,
        "Target tests (must pass):",
        *[f"- {node}" for node in oracle.target_tests],
        f"Regression check: {oracle.regression}. Python {oracle.python_version}.",
        "Protected paths (never edit): " + ", ".join(protected[:30]),
        "</task>",
    ]
    if spec.description.strip():
        lines.append(untrusted("description", spec.description.strip()))
    return "\n".join(lines)


def tests_block(
    read: Callable[[str], str | None],
    target_tests: list[str],
    budget_tokens: int = 8000,  # noqa: PT028 - not a test: this builds the prompt block of target tests
) -> str:
    """The target test files, read-only. Large files keep the failing test functions in full."""
    budget_chars = int(budget_tokens * 3.5 / 1.15)
    by_file: dict[str, list[str]] = {}
    for node in target_tests:
        path, _, rest = node.partition("::")
        by_file.setdefault(path, []).append(rest.split("[", 1)[0])
    sections = []
    per_file = max(2000, budget_chars // max(1, len(by_file)))
    for path, names in sorted(by_file.items()):
        text = read(path)
        if text is None:
            sections.append(f"TEST FILE {path}: not found in the repository")
            continue
        text = safe_text(path, text)
        if len(text) > per_file:
            text = _focus_tests(text, {n.rsplit("::", 1)[-1] for n in names}, per_file)
        sections.append(f"TEST FILE {path} (read-only)\n" + untrusted("test", text, path))
    return "\n\n".join(sections)


def _focus_tests(text: str, names: set[str], limit: int) -> str:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text[:limit]
    lines = text.splitlines()
    keep: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            start = min([d.lineno for d in node.decorator_list] + [node.lineno])
            keep.append("\n".join(lines[start - 1 : node.end_lineno or node.lineno]))
    header = "\n".join(line for line in lines[:60] if line.startswith(("import ", "from ")))
    others = [f"# def {i}" for i in outline_python(text, 80)]
    return (header + "\n\n" + "\n\n".join(keep) + "\n\n# other tests in this file:\n" + "\n".join(others))[
        :limit
    ]


def history_block(
    attempts: list[AttemptRecord], last_diff: str | None, best_diff: str | None, refused: list[str]
) -> str:
    if not attempts:
        return ""
    lines = ["<attempt_history>"]
    for attempt in attempts:
        lines.append(f"Attempt {attempt.attempt_no} ({attempt.kind.value}, model {attempt.model or '?'}):")
        if attempt.plan and attempt.plan.get("hypothesis"):
            lines.append(f"  hypothesis: {str(attempt.plan['hypothesis'])[:400]}")
        if attempt.diagnosis:
            lines.append(f"  diagnosis: {attempt.diagnosis[:400]}")
        files = sorted(attempt.files_after)
        if files:
            lines.append(f"  changed: {', '.join(files[:10])}")
        verdict = attempt.verdict
        if verdict:
            lines.append(f"  result: {verdict.message[:300]}")
            if verdict.regressions:
                lines.append(f"  regressions: {', '.join(verdict.regressions[:8])}")
        if attempt.failure_class:
            lines.append(
                f"  failure class: {attempt.failure_class.value}"
                + ("  (the SAME failure as an earlier attempt)" if attempt.signature_repeated else "")
            )
    if refused:
        lines.append("Refused dependency requests: " + "; ".join(refused))
    lines.append("</attempt_history>")
    if last_diff:
        lines.append("Diff of the LAST attempt (it did not pass):\n" + untrusted("diff", last_diff[:12000]))
    if best_diff and best_diff != last_diff:
        lines.append(
            "Diff of the BEST attempt so far (the current files below include it):\n"
            + untrusted("diff", best_diff[:12000])
        )
    return "\n".join(lines)


def current_state_block(overlay: dict[str, str | None], budget_tokens: int = 12000) -> str:
    if not overlay:
        return "<current_state>No changes are kept so far: the files shown above are current.</current_state>"
    budget_chars = int(budget_tokens * 3.5 / 1.15)
    parts = [
        "<current_state>The best attempt so far changed these files. Their CURRENT content is below; "
        "anchor your edits on this content, not on the original versions above."
    ]
    used = 0
    for path in sorted(overlay):
        text = overlay[path]
        if text is None:
            parts.append(f"DELETED {path}")
            continue
        rendered = safe_text(path, text)[: max(1000, budget_chars - used)]
        parts.append(f"CURRENT FILE {path}\n" + untrusted("file", rendered, path))
        used += len(rendered)
    parts.append("</current_state>")
    return "\n".join(parts)


def failure_block(trace_text: str, digest_summary: str | None = None) -> str:
    parts = ["<failure>"]
    if digest_summary:
        parts.append("Summary: " + digest_summary)
    parts.append(untrusted("trace", trace_text))
    parts.append("</failure>")
    return "\n".join(parts)


def baseline_failure_block(summary: TraceSummary, trace_text: str) -> str:
    frame = top_repo_frame(summary.failures[0].frames) if summary.failures else None
    where = f" The crash is near {frame.path}:{frame.line}." if frame else ""
    return failure_block(
        trace_text, f"The target tests fail on the untouched code ({summary.failure_class.value}).{where}"
    )


# ------------------------------------------------------------------------------------ sql blocks
DIALECT_NOTES = {
    "postgres": (
        "integer / integer truncates (cast to numeric for ratios); 'text' || NULL is NULL; "
        "unquoted identifiers fold to lower case; use date_trunc and interval arithmetic for dates"
    ),
    "duckdb": (
        "/ always returns a floating-point number (use // for integer division); 'text' || NULL is NULL; "
        "identifiers are case-insensitive; use date_trunc, strftime and INTERVAL for dates"
    ),
}


def sql_frame(spec: TaskSpec) -> str:
    oracle = spec.oracle
    assert isinstance(oracle, SqlOracle)
    goal = (
        "Write a query that answers the question."
        if spec.kind == "sql.write"
        else "Fix the query below so it answers the question correctly."
    )
    lines = [f'<task kind="{spec.kind}" dialect="{oracle.dialect}">', goal, "</task>"]
    if spec.description.strip():
        lines.append(untrusted("question", spec.description.strip()))
    if oracle.broken_query:
        lines.append("Current (broken) query:\n" + untrusted("query", oracle.broken_query))
    return "\n".join(lines)


def schema_block(schema: dict | None, ddl: str) -> str:
    """DDL plus per-column statistics computed by the sandbox when it built the snapshot database."""
    parts = ["<schema>", untrusted("ddl", ddl.strip()[:20000])]
    tables = (schema or {}).get("tables", [])
    for table in tables[:200]:
        lines = [f"table {table.get('name')}: {table.get('row_count', '?')} rows"]
        for column in table.get("columns", [])[:200]:
            stats = column.get("stats", {})
            samples = ", ".join(repr(s) for s in (stats.get("samples") or [])[:5])
            lines.append(
                f"  {column.get('name')} {column.get('type')}"
                f" distinct={stats.get('distinct', '?')} nulls={stats.get('null_fraction', '?')}"
                f" min={stats.get('min')!r} max={stats.get('max')!r}"
                + (f" samples=[{samples}]" if samples else "")
            )
        parts.append(untrusted("table_stats", "\n".join(lines)))
    parts.append("</schema>")
    return "\n".join(parts)


def sql_requirements(oracle: SqlOracle) -> tuple[list[str], list[str]]:
    """Visible requirements (shape properties) and expected output columns. Values stay hidden."""
    requirements: list[str] = []
    for prop in oracle.properties:
        if not prop.visible:
            continue
        data = prop.model_dump()
        kind = data.pop("kind")
        if kind == "columns":
            requirements.append("columns: " + ", ".join(data["names"]))
        elif kind == "unique":
            requirements.append("rows must be unique on: " + ", ".join(data["columns"]))
        elif kind == "not_null":
            requirements.append(f"column {data['column']} must never be NULL")
        elif kind == "sorted_by":
            requirements.append(f"rows sorted by {', '.join(data['columns'])} {data['direction']}")
    if oracle.order_matters and oracle.sort_key:
        requirements.append("rows must be ordered by " + ", ".join(oracle.sort_key))
    expected_columns: list[str] = []
    if oracle.expected is not None:
        expected_columns = list(oracle.expected.columns)
    for prop in oracle.properties:
        if prop.kind == "columns" and not expected_columns:
            expected_columns = list(prop.names)  # type: ignore[union-attr]
    return requirements, expected_columns


def sql_history_block(attempts: list[AttemptRecord]) -> str:
    if not attempts:
        return ""
    lines = ["<attempt_history>"]
    for attempt in attempts:
        lines.append(f"Attempt {attempt.attempt_no} ({attempt.kind.value}):")
        if attempt.query:
            lines.append(untrusted("query", attempt.query[:4000]))
        if attempt.verdict:
            lines.append(f"  result: {attempt.verdict.message[:600]}")
        if attempt.signature_repeated:
            lines.append("  (the SAME problem as an earlier attempt)")
    lines.append("</attempt_history>")
    return "\n".join(lines)
