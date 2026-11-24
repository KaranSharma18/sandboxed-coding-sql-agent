"""Repository structure without running anything: outlines, a module index and an import graph.

Everything here uses `ast` on source text. Nothing from the repository is ever imported or
executed in the worker: import resolution is purely static.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from functools import lru_cache

from fixloop_core.traces import is_test_path

ROOT_PREFIXES = ("", "src/", "lib/")


def outline_python(text: str, max_items: int = 60) -> list[str]:
    """Top-level classes (with their methods) and functions, as short signature lines."""
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [f"<does not parse: line {exc.lineno}: {exc.msg}>"]
    items: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            items.append(
                f"{'async ' if isinstance(node, ast.AsyncFunctionDef) else ''}def {node.name}"
                f"({_args(node.args)})  # line {node.lineno}"
            )
        elif isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            shown = ", ".join(methods[:20]) + (" ..." if len(methods) > 20 else "")
            items.append(
                f"class {node.name}({bases})  # line {node.lineno}" + (f": {shown}" if methods else "")
            )
        if len(items) >= max_items:
            items.append("...")
            break
    return items


def _args(args: ast.arguments) -> str:
    names = [a.arg for a in args.posonlyargs + args.args]
    if args.vararg:
        names.append("*" + args.vararg.arg)
    names += [a.arg for a in args.kwonlyargs]
    if args.kwarg:
        names.append("**" + args.kwarg.arg)
    text = ", ".join(names)
    return text if len(text) <= 80 else text[:77] + "..."


def build_repo_map(paths: list[str], read: Callable[[str], str | None], budget_chars: int = 16000) -> str:
    """File tree with outlines of non-test Python files, shrinking gracefully to fit the budget."""
    py_files = [p for p in paths if p.endswith(".py")]
    others = [p for p in paths if not p.endswith(".py")]
    outlines: dict[str, list[str]] = {}
    for path in py_files:
        if is_test_path(path):
            continue
        text = read(path)
        if text is not None:
            outlines[path] = outline_python(text, max_items=25)

    def render(with_outlines: set[str], collapse_others: bool) -> str:
        lines: list[str] = []
        for path in py_files:
            lines.append(path)
            if path in with_outlines:
                lines.extend("    " + item for item in outlines.get(path, []))
        if collapse_others:
            by_dir: dict[str, int] = {}
            for path in others:
                by_dir[path.rsplit("/", 1)[0] if "/" in path else "."] = (
                    by_dir.get(path.rsplit("/", 1)[0] if "/" in path else ".", 0) + 1
                )
            lines += [f"{d}/ ({n} non-Python files)" for d, n in sorted(by_dir.items())]
        else:
            lines += others
        return "\n".join(lines)

    with_outlines = set(outlines)
    text = render(with_outlines, collapse_others=False)
    if len(text) <= budget_chars:
        return text
    text = render(with_outlines, collapse_others=True)
    # Drop outlines from the deepest files first until it fits.
    for path in sorted(with_outlines, key=lambda p: (-p.count("/"), p)):
        if len(text) <= budget_chars:
            break
        with_outlines.discard(path)
        text = render(with_outlines, collapse_others=True)
    if len(text) > budget_chars:
        text = text[: budget_chars - 40] + "\n... (repository map truncated)"
    return text


# ------------------------------------------------------------------------------------ imports
def module_names_for(path: str) -> list[str]:
    if not path.endswith(".py"):
        return []
    names = []
    for prefix in ROOT_PREFIXES:
        if prefix and not path.startswith(prefix):
            continue
        rel = path[len(prefix) :][:-3]
        parts = rel.split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts and all(p.isidentifier() for p in parts):
            names.append(".".join(parts))
    return names


def module_index(paths: list[str]) -> dict[str, str]:
    index: dict[str, str] = {}
    for path in sorted(paths, key=lambda p: (p.count("/"), p)):
        for name in module_names_for(path):
            index.setdefault(name, path)
    return index


def _package_of(path: str) -> list[str]:
    names = module_names_for(path)
    if not names:
        return []
    parts = names[-1].split(".")
    return parts if path.endswith("__init__.py") else parts[:-1]


def imported_modules(path: str, text: str) -> set[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    found: set[str] = set()
    package = _package_of(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)] if node.level - 1 <= len(package) else []
                module = ".".join([*base, node.module] if node.module else base)
            else:
                module = node.module or ""
            if module:
                found.add(module)
                for alias in node.names:
                    found.add(f"{module}.{alias.name}")
    return found


def resolve_imports(path: str, text: str, index: dict[str, str]) -> set[str]:
    targets: set[str] = set()
    for module in imported_modules(path, text):
        parts = module.split(".")
        while parts:
            hit = index.get(".".join(parts))
            if hit:
                if hit != path:
                    targets.add(hit)
                break
            parts.pop()
    return targets


@lru_cache(maxsize=64)
def _cached_graph(key: str, items: tuple[tuple[str, str], ...]) -> dict[str, frozenset[str]]:
    index = module_index([p for p, _ in items])
    return {path: frozenset(resolve_imports(path, text, index)) for path, text in items}


def import_graph(files: dict[str, str], cache_key: str = "") -> dict[str, frozenset[str]]:
    """path -> repository files it imports (one hop). Cached per snapshot when a key is given."""
    items = tuple(sorted((p, t) for p, t in files.items() if p.endswith(".py")))
    if cache_key:
        return _cached_graph(cache_key, items)
    index = module_index([p for p, _ in items])
    return {path: frozenset(resolve_imports(path, text, index)) for path, text in items}


def importers_of(changed: set[str], graph: dict[str, frozenset[str]]) -> set[str]:
    return {path for path, deps in graph.items() if deps & changed}
