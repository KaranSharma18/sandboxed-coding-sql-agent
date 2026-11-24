"""Applying the model's anchored edits, and turning results into fingerprints and diffs.

Why anchored search/replace (ADR-06): anchors survive line shifts, are unambiguous when they
match exactly once, and a failed anchor gives precise feedback for a cheap re-ask.

Rules implemented here:
- Files are matched with LF line endings and written back in their original style, so CRLF
  files edited on Windows stay CRLF and a diff never shows every line as changed.
- An anchor must match exactly once. If it does not match, a tolerant match that ignores
  trailing whitespace and surrounding blank lines is tried; a unique tolerant match is applied
  and flagged `fuzzy`. Otherwise the error quotes the most similar region of the file.
- Edits to one file are located against that file as it was before this patch. Two edits whose
  regions overlap fail the patch (the model must merge them into one edit).
"""

from __future__ import annotations

import ast
import difflib
import itertools
from collections.abc import Mapping
from dataclasses import dataclass, field

from fixloop_core.hashing import sha256_bytes, stable_hash
from fixloop_core.outputs import Edit


@dataclass
class ApplyError:
    # anchor_not_found | anchor_ambiguous | overlap | file_missing | file_exists | not_text | syntax_error
    code: str
    path: str
    message: str
    edit_index: int | None = None
    hint: str = ""

    def render(self) -> str:
        where = f"edit {self.edit_index + 1}, " if self.edit_index is not None else ""
        text = f"[{self.code}] {where}{self.path}: {self.message}"
        return f"{text}\n{self.hint}" if self.hint else text


class PatchApplyFailed(Exception):
    def __init__(self, errors: list[ApplyError]) -> None:
        super().__init__("; ".join(e.render() for e in errors))
        self.errors = errors


@dataclass
class AppliedPatch:
    files_after: dict[str, str | None]  # changed paths -> new text (with original EOLs); None = deleted
    fingerprint: str
    changed_lines: int
    fuzzy_edits: list[int] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    def encoded(self) -> dict[str, bytes | None]:
        return {p: (t.encode("utf-8") if t is not None else None) for p, t in self.files_after.items()}


class WorkingTree:
    """The repository as the next attempt sees it: snapshot files plus an overlay of changes."""

    def __init__(
        self,
        base: Mapping[str, bytes],
        overlay: Mapping[str, str | None] | None = None,
        eol: Mapping[str, str] | None = None,
    ) -> None:
        self._base = base
        self._overlay: dict[str, str | None] = dict(overlay or {})
        self._eol = dict(eol or {})

    @property
    def overlay(self) -> dict[str, str | None]:
        return dict(self._overlay)

    def exists(self, path: str) -> bool:
        if path in self._overlay:
            return self._overlay[path] is not None
        return path in self._base

    def read(self, path: str) -> str | None:
        """Current text with its original line endings, or None if missing or not UTF-8 text."""
        if path in self._overlay:
            return self._overlay[path]
        data = self._base.get(path)
        if data is None or b"\x00" in data[:8192]:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def read_base(self, path: str) -> str | None:
        """The original snapshot's text, ignoring the overlay."""
        data = self._base.get(path)
        if data is None or b"\x00" in data[:8192]:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def eol(self, path: str) -> str:
        return self._eol.get(path, "lf")

    def paths(self) -> list[str]:
        present = {p for p in self._base if self._overlay.get(p, "") is not None}
        present |= {p for p, t in self._overlay.items() if t is not None}
        return sorted(present)

    def with_overlay(self, changes: Mapping[str, str | None]) -> WorkingTree:
        merged = dict(self._overlay)
        merged.update(changes)
        return WorkingTree(self._base, merged, self._eol)


def _to_lf(text: str) -> str:
    return text.replace("\r\n", "\n")


def _from_lf(text: str, eol: str) -> str:
    return text.replace("\n", "\r\n") if eol == "crlf" else text


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _tolerant_find(text: str, search: str) -> list[tuple[int, int]]:
    """Find `search` ignoring trailing whitespace per line and blank lines at its edges."""
    wanted = [line.rstrip() for line in search.split("\n")]
    while wanted and not wanted[0]:
        wanted.pop(0)
    while wanted and not wanted[-1]:
        wanted.pop()
    if not wanted:
        return []
    lines = text.split("\n")
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line) + 1)
    stripped = [line.rstrip() for line in lines]
    width = len(wanted)
    spans: list[tuple[int, int]] = []
    for start in range(len(lines) - width + 1):
        if stripped[start : start + width] == wanted:
            end_line = start + width - 1
            spans.append((offsets[start], offsets[end_line] + len(lines[end_line])))
    return spans


def _closest_region(text: str, search: str, context: int = 0) -> str:
    lines = text.split("\n")
    width = max(1, len(search.split("\n")))
    best_ratio, best_start = 0.0, 0
    target = search.strip()
    step = max(1, len(lines) // 400)  # keep this cheap on very large files
    for start in range(0, max(1, len(lines) - width + 1), step):
        window = "\n".join(lines[start : start + width])
        ratio = difflib.SequenceMatcher(None, window.strip(), target, autojunk=False).quick_ratio()
        if ratio > best_ratio:
            best_ratio, best_start = ratio, start
    if best_ratio < 0.3:
        return ""
    snippet = "\n".join(lines[max(0, best_start - context) : best_start + width + context])
    return f"The most similar region starts at line {best_start + 1}:\n{snippet}"


def _locate(text: str, edit: Edit, index: int, errors: list[ApplyError]) -> tuple[int, int, bool] | None:
    search = _to_lf(edit.search or "")
    count = text.count(search)
    if count == 1:
        start = text.index(search)
        return start, start + len(search), False
    if count > 1:
        lines, cursor = [], 0
        while (found := text.find(search, cursor)) != -1 and len(lines) < 10:
            lines.append(_line_of(text, found))
            cursor = found + 1
        errors.append(
            ApplyError(
                "anchor_ambiguous",
                edit.path,
                f"`search` matches {count} times (lines {', '.join(map(str, lines))}); "
                "include more surrounding lines so it matches exactly once",
                index,
            )
        )
        return None
    spans = _tolerant_find(text, search)
    if len(spans) == 1:
        return spans[0][0], spans[0][1], True
    if len(spans) > 1:
        errors.append(
            ApplyError(
                "anchor_ambiguous",
                edit.path,
                f"`search` matches {len(spans)} places when trailing whitespace is ignored",
                index,
            )
        )
        return None
    errors.append(
        ApplyError(
            "anchor_not_found",
            edit.path,
            "`search` does not occur in the current file",
            index,
            _closest_region(text, search),
        )
    )
    return None


def apply_edits(tree: WorkingTree, edits: list[Edit], *, check_syntax: bool = True) -> AppliedPatch:
    """Apply edits to a working tree, returning the new contents of every changed file."""
    errors: list[ApplyError] = []
    fuzzy: list[int] = []
    results: dict[str, str | None] = {}
    created: list[str] = []
    deleted: list[str] = []
    per_file: dict[str, list[tuple[int, Edit]]] = {}

    for index, edit in enumerate(edits):
        if edit.action == "create":
            if tree.exists(edit.path) or edit.path in results:
                errors.append(
                    ApplyError("file_exists", edit.path, "cannot create a file that already exists", index)
                )
                continue
            results[edit.path] = _to_lf(edit.replace or "")
            created.append(edit.path)
        elif edit.action == "delete":
            if not tree.exists(edit.path):
                errors.append(
                    ApplyError("file_missing", edit.path, "cannot delete a file that does not exist", index)
                )
                continue
            results[edit.path] = None
            deleted.append(edit.path)
        else:
            per_file.setdefault(edit.path, []).append((index, edit))

    for path, file_edits in per_file.items():
        if path in results:
            errors.append(
                ApplyError("overlap", path, "a file cannot be created or deleted and edited in one patch")
            )
            continue
        original = tree.read(path)
        if original is None:
            code = "file_missing" if not tree.exists(path) else "not_text"
            message = (
                "file does not exist"
                if code == "file_missing"
                else "file is binary or not UTF-8; it cannot be edited"
            )
            errors.append(ApplyError(code, path, message, file_edits[0][0]))
            continue
        text = _to_lf(original)
        spans: list[tuple[int, int, int, Edit]] = []
        for index, edit in file_edits:
            located = _locate(text, edit, index, errors)
            if located is None:
                continue
            start, end, was_fuzzy = located
            if was_fuzzy:
                fuzzy.append(index)
            spans.append((start, end, index, edit))
        spans.sort()
        for (_s1, e1, i1, _), (s2, _e2, i2, _) in itertools.pairwise(spans):
            if s2 < e1:
                errors.append(
                    ApplyError(
                        "overlap",
                        path,
                        f"edits {i1 + 1} and {i2 + 1} touch overlapping text; merge them into one edit",
                        i2,
                    )
                )
        if errors:
            continue
        new_text = text
        for start, end, _index, edit in sorted(spans, reverse=True):
            region = new_text[start:end]
            replacement = _to_lf(edit.replace or "")
            if edit.action == "replace":
                piece = replacement
            elif edit.action == "insert_before":
                piece = replacement + region
            else:
                piece = region + replacement
            new_text = new_text[:start] + piece + new_text[end:]
        if text.endswith("\n") and not new_text.endswith("\n"):
            new_text += "\n"
        results[path] = new_text

    if errors:
        raise PatchApplyFailed(errors)

    if check_syntax:
        for path, text in results.items():
            if text is not None and path.endswith(".py"):
                try:
                    ast.parse(text, filename=path)
                except SyntaxError as exc:
                    errors.append(
                        ApplyError(
                            "syntax_error", path, f"{exc.msg} (line {exc.lineno}, column {exc.offset})"
                        )
                    )
        if errors:
            raise PatchApplyFailed(errors)

    final: dict[str, str | None] = {}
    changed_lines = 0
    for path, text in results.items():
        old = tree.read(path)
        if text is None:
            final[path] = None
            changed_lines += len((old or "").splitlines())
            continue
        restored = _from_lf(text, tree.eol(path) if old is not None else "lf")
        if old is not None and _to_lf(old) == text:
            continue  # a no-op edit: nothing changed in this file
        final[path] = restored
        changed_lines += count_changed_lines(old or "", restored)
    return AppliedPatch(final, fingerprint_of(tree, final), changed_lines, fuzzy, created, deleted)


def count_changed_lines(old: str, new: str) -> int:
    changed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        None, old.splitlines(), new.splitlines(), autojunk=False
    ).get_opcodes():
        if tag != "equal":
            changed += (i2 - i1) + (j2 - j1)
    return changed


def fingerprint_of(tree: WorkingTree, changes: Mapping[str, str | None]) -> str:
    """Identity of the resulting code: equal fingerprints mean byte-identical changed files."""
    merged = tree.overlay
    merged.update(changes)
    items = sorted(
        (path, sha256_bytes(text.encode("utf-8")) if text is not None else "deleted")
        for path, text in merged.items()
    )
    return stable_hash("fingerprint-v1", items)


def unified_diff(tree: WorkingTree, changes: Mapping[str, str | None]) -> str:
    """A `git diff`-style patch from the original snapshot to the changed state (git apply compatible)."""
    out: list[str] = []
    for path in sorted(changes):
        new = changes[path]
        base = tree.read_base(path)
        if base == new:
            continue
        old_lines = (base or "").splitlines(keepends=True)
        new_lines = (new or "").splitlines(keepends=True)
        out.append(f"diff --git a/{path} b/{path}\n")
        if base is None:
            out.append("new file mode 100644\n")
        elif new is None:
            out.append("deleted file mode 100644\n")
        fromfile = f"a/{path}" if base is not None else "/dev/null"
        tofile = f"b/{path}" if new is not None else "/dev/null"
        for line in difflib.unified_diff(old_lines, new_lines, fromfile, tofile, n=3, lineterm="\n"):
            if (
                line[:1] in (" ", "+", "-")
                and not line.startswith(("--- ", "+++ "))
                and not line.endswith("\n")
            ):
                out.append(line + "\n\\ No newline at end of file\n")
            else:
                out.append(line)
    return "".join(out)
