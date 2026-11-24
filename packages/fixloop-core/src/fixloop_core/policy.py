"""Deterministic policy checks on a candidate patch, before anything runs.

Every violation carries a stable code and a plain message that goes back to the model on a
re-ask. The test-gaming checks walk the AST of *added* code rather than matching text, so a
comment that mentions `pytest.skip` is fine while `from pytest import skip as s` is not.
"""

from __future__ import annotations

import ast
import configparser
import re
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache

from fixloop_core.config import PatchPolicy, normalize_project_name
from fixloop_core.outputs import DependencyRequest, Edit
from fixloop_core.patching import AppliedPatch, WorkingTree
from fixloop_core.secrets import contains_redaction


@dataclass(frozen=True)
class Violation:
    code: str
    path: str
    message: str

    def render(self) -> str:
        return f"[{self.code}] {self.path}: {self.message}"


@dataclass
class PolicyResult:
    violations: list[Violation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    approved_dependencies: list[DependencyRequest] = field(default_factory=list)
    refused_dependencies: list[tuple[DependencyRequest, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def feedback(self) -> str:
        lines = [v.render() for v in self.violations]
        lines += [
            f"[dependency refused] {d.package}=={d.version}: {why}" for d, why in self.refused_dependencies
        ]
        return "\n".join(lines)


# ------------------------------------------------------------------------------------ globs
@lru_cache(maxsize=512)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    """`**/` matches any number of directories, `*` stays within one path segment."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(f"^{out}$")


def glob_match(path: str, pattern: str) -> bool:
    return bool(_glob_regex(pattern).match(path))


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(glob_match(path, p) for p in patterns)


def protected_paths_for(policy: PatchPolicy, extra: list[str], target_tests: list[str]) -> list[str]:
    target_files = sorted({node.split("::", 1)[0] for node in target_tests})
    return [*policy.protected_globs, *extra, *target_files]


# ------------------------------------------------------------------------------------ AST helpers
def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return None


class _Facts(ast.NodeVisitor):
    """Collect the facts the gaming and risk rules count, with import aliases resolved."""

    def __init__(self) -> None:
        self.aliases: dict[str, str] = {}
        self.calls: Counter[str] = Counter()
        self.decorators: Counter[str] = Counter()
        self.imports_from: Counter[tuple[str, str]] = Counter()
        self.modules: Counter[str] = Counter()
        self.strings: list[str] = []
        self.sys_modules_keys: Counter[str] = Counter()

    def _resolve(self, dotted: str | None) -> str | None:
        if not dotted:
            return None
        head, _, rest = dotted.partition(".")
        base = self.aliases.get(head, head)
        return f"{base}.{rest}" if rest else base

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.modules[alias.name] += 1
            self.aliases[alias.asname or alias.name.split(".")[0]] = (
                alias.name if alias.asname else alias.name.split(".")[0]
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        self.modules[module] += 1
        for alias in node.names:
            self.imports_from[(module, alias.name)] += 1
            self.aliases[alias.asname or alias.name] = f"{module}.{alias.name}" if module else alias.name

    def visit_Call(self, node: ast.Call) -> None:
        name = self._resolve(_dotted(node.func))
        if name:
            self.calls[name] += 1
        self.generic_visit(node)

    def _decorated(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            name = self._resolve(_dotted(decorator))
            if name:
                self.decorators[name] += 1
        self.generic_visit(node)

    visit_FunctionDef = _decorated
    visit_AsyncFunctionDef = _decorated
    visit_ClassDef = _decorated

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self.strings.append(node.value)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if self._resolve(_dotted(node.value)) == "sys.modules" and isinstance(node.slice, ast.Constant):
            self.sys_modules_keys[str(node.slice.value)] += 1
        self.generic_visit(node)


def _facts(text: str | None) -> _Facts:
    facts = _Facts()
    if text:
        try:
            facts.visit(ast.parse(text))
        except SyntaxError:
            pass
    return facts


def _increased(new: Counter, old: Counter) -> list:  # type: ignore[type-arg]
    return [key for key, count in new.items() if count > old.get(key, 0)]


def _string_increased(new: list[str], old: list[str], needle: str) -> bool:
    return sum(needle in s for s in new) > sum(needle in s for s in old)


# ------------------------------------------------------------------------------------ config-file rules
def _toml_tables(text: str | None, prefixes: list[str]) -> dict[str, object]:
    if not text:
        return {}
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {"__unparseable__": True}
    found: dict[str, object] = {}
    for prefix in prefixes:
        node: object = data
        for part in prefix.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if node is not None:
            found[prefix] = node
    return found


def _project_dependencies(text: str | None) -> object:
    if not text:
        return None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    project = data.get("project", {})
    return (project.get("dependencies"), project.get("optional-dependencies"), data.get("dependency-groups"))


def _cfg_sections(text: str | None, sections: list[str]) -> dict[str, dict[str, str]]:
    if not text:
        return {}
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        return {"__unparseable__": {}}
    return {s: dict(parser.items(s)) for s in sections if parser.has_section(s)}


# ------------------------------------------------------------------------------------ the check
def check_patch(
    *,
    tree: WorkingTree,
    edits: list[Edit],
    applied: AppliedPatch,
    policy: PatchPolicy,
    protected: list[str],
    task_kind: str,
    allow_dependency_changes: bool,
    dependency_requests: list[DependencyRequest],
    allowlist: frozenset[str],
    target_test_files: list[str],
) -> PolicyResult:
    result = PolicyResult()
    add = result.violations.append

    for edit in edits:
        if edit.path.split("/")[0] == ".git" or "/.git/" in f"/{edit.path}":
            add(Violation("path_outside_root", edit.path, "the .git directory cannot be edited"))
        if contains_redaction(edit.search or "") or contains_redaction(edit.replace or ""):
            add(Violation("redacted_region", edit.path, "edits may not touch redacted (secret) regions"))

    for path in applied.files_after:
        if matches_any(path, protected):
            add(Violation("protected_path", path, "tests and test configuration may not be edited"))
        elif matches_any(path, policy.protected_infra_globs):
            add(
                Violation(
                    "protected_infra",
                    path,
                    "CI, container, hook and dependency files may not be edited; "
                    "use a dependency request for a missing package",
                )
            )

    # pyproject.toml / setup.cfg may change, except the tables that configure the oracle.
    for path in applied.files_after:
        name = path.rsplit("/", 1)[-1]
        old, new = tree.read(path), applied.files_after[path]
        if name == "pyproject.toml":
            if _toml_tables(old, policy.protected_pyproject_tables) != _toml_tables(
                new, policy.protected_pyproject_tables
            ):
                add(
                    Violation(
                        "protected_path", path, "the [tool.pytest] and [tool.coverage] tables may not change"
                    )
                )
            if _project_dependencies(old) != _project_dependencies(new):
                add(
                    Violation(
                        "dependency_not_allowed",
                        path,
                        "dependencies may not be edited directly; use a dependency request",
                    )
                )
        elif name == "setup.cfg" and _cfg_sections(old, policy.protected_setup_cfg_sections) != _cfg_sections(
            new, policy.protected_setup_cfg_sections
        ):
            add(Violation("protected_path", path, "test and coverage sections of setup.cfg may not change"))

    rules = policy.test_gaming
    for path, new_text in applied.files_after.items():
        if not path.endswith(".py") or new_text is None:
            continue
        old_f, new_f = _facts(tree.read(path)), _facts(new_text)
        bad: list[str] = []
        bad += [f"call to {c}" for c in _increased(new_f.calls, old_f.calls) if c in rules.forbidden_calls]
        bad += [
            f"decorator {d}"
            for d in _increased(new_f.decorators, old_f.decorators)
            if any(d == f or d.startswith(f + ".") for f in rules.forbidden_decorators)
        ]
        for module, name in _increased(new_f.imports_from, old_f.imports_from):
            if name in rules.forbidden_imports_from.get(module, []):
                bad.append(f"import of {module}.{name}")
        for module in _increased(new_f.modules, old_f.modules):
            if module.split(".")[0] in rules.forbidden_modules:
                bad.append(f"import of {module}")
        for needle in rules.forbidden_strings:
            if _string_increased(new_f.strings, old_f.strings, needle):
                bad.append(f"reference to {needle!r}")
        bad += [
            f"sys.modules[{k!r}]"
            for k in _increased(new_f.sys_modules_keys, old_f.sys_modules_keys)
            if k.split(".")[0] in ("pytest", "_pytest", "fixloop_pytest")
        ]
        for test_file in target_test_files:
            if _string_increased(new_f.strings, old_f.strings, test_file):
                bad.append(f"reference to the test file {test_file!r}")
        if bad:
            add(
                Violation(
                    "test_gaming",
                    path,
                    "added code may not skip, exit, detect or read tests: " + ", ".join(bad),
                )
            )
        risky = [
            m for m in _increased(new_f.modules, old_f.modules) if m.split(".")[0] in policy.risky_modules
        ]
        risky += [c for c in _increased(new_f.calls, old_f.calls) if c in policy.risky_calls]
        if risky:
            result.warnings.append(f"{path}: new use of {', '.join(sorted(set(risky)))}")

    limits = policy.limits
    max_lines = (
        limits.max_changed_lines_implement if task_kind == "python.implement" else limits.max_changed_lines
    )
    if len(edits) > limits.max_edits:
        add(Violation("patch_too_large", "*", f"{len(edits)} edits; at most {limits.max_edits} are allowed"))
    if len(applied.files_after) > limits.max_files:
        add(
            Violation(
                "patch_too_large",
                "*",
                f"{len(applied.files_after)} files changed; at most {limits.max_files}",
            )
        )
    if applied.changed_lines > max_lines:
        add(Violation("patch_too_large", "*", f"{applied.changed_lines} changed lines; at most {max_lines}"))
    if len(applied.deleted) > limits.max_deletes:
        add(
            Violation(
                "delete_not_allowed", "*", f"{len(applied.deleted)} deletions; at most {limits.max_deletes}"
            )
        )

    for request in dependency_requests:
        if not allow_dependency_changes:
            result.refused_dependencies.append((request, "dependency changes are not enabled for this task"))
        elif normalize_project_name(request.package) not in allowlist:
            result.refused_dependencies.append((request, "the package is not on the allowlist"))
        else:
            result.approved_dependencies.append(request)

    for path in applied.fuzzy_edits:
        result.warnings.append(f"edit {path + 1} matched with whitespace tolerance")
    return result
