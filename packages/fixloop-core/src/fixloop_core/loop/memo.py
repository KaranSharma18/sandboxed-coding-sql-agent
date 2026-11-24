"""L2 (verified fixes) and L4 (failure memo) cache entries for Python tasks (ADR-18).

Both layers store *answers*, so a hit is never trusted: it is turned into an ordinary candidate
and verified in the sandbox exactly like a model's patch. A hit that fails verification is
deleted and the task continues as if nothing had been cached.

* L2 is keyed on the exact snapshot, oracle and environment: the stored overlay applies as is.
* L4 is keyed on the baseline failure signature, oracle and environment, so it can help a
  *different* snapshot that fails the same way. Its value is a set of anchored edits; the edits
  applying cleanly (every anchor found exactly once, the result still parses) is the
  precondition for trying it at all.
"""

from __future__ import annotations

import difflib
from typing import Any

from fixloop_core.cachekeys import cache_key
from fixloop_core.outputs import Edit

CONTEXT_LINES = 2


def fix_key(tenant_id: str, kind: str, snapshot_sha: str, oracle_hash: str, env_hash: str) -> str:
    return cache_key("fix", tenant_id, kind, snapshot_sha, oracle_hash, env_hash)


def memo_key(tenant_id: str, kind: str, signature: str, oracle_hash: str, env_hash: str) -> str:
    return cache_key("memo", tenant_id, kind, signature, oracle_hash, env_hash)


def sql_fix_key(tenant_id: str, kind: str, env_hash: str, oracle_hash: str, question_hash: str) -> str:
    return cache_key("fix", tenant_id, kind, env_hash, oracle_hash, question_hash)


def edits_from_change(path: str, old: str | None, new: str | None) -> list[Edit]:
    """Anchored edits that turn `old` into `new`, each anchored on a little surrounding context."""
    if old is None and new is not None:
        return [Edit(path=path, action="create", replace=new, reason="cached fix")]
    if new is None:
        return [Edit(path=path, action="delete", reason="cached fix")]
    assert old is not None
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    edits: list[Edit] = []
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for group in matcher.get_grouped_opcodes(CONTEXT_LINES):
        a_start, a_end = group[0][1], group[-1][2]
        b_start, b_end = group[0][3], group[-1][4]
        search = "".join(old_lines[a_start:a_end])
        replace = "".join(new_lines[b_start:b_end])
        if not search.strip():
            # Nothing to anchor on (only blank lines around the change): replace the whole file.
            if not old.strip():
                raise ValueError(f"{path}: an empty file cannot be anchored")
            return [Edit(path=path, action="replace", search=old, replace=new, reason="cached fix")]
        edits.append(Edit(path=path, action="replace", search=search, replace=replace, reason="cached fix"))
    return edits


def overlay_to_edits(base_read: Any, overlay: dict[str, str | None]) -> list[dict[str, Any]]:
    edits: list[dict[str, Any]] = []
    for path in sorted(overlay):
        for edit in edits_from_change(path, base_read(path), overlay[path]):
            edits.append(edit.model_dump(exclude_none=True))
    return edits
