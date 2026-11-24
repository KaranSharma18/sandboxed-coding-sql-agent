import subprocess
from pathlib import Path

import pytest

from fixloop_core.outputs import Edit
from fixloop_core.patching import PatchApplyFailed, WorkingTree, apply_edits, unified_diff

SOURCE = b"""def add(a, b):
    return a - b


def sub(a, b):
    return a - b
"""


def tree(files=None, eol=None):
    return WorkingTree(files or {"calc.py": SOURCE}, eol=eol or {})


def test_exact_replace():
    result = apply_edits(
        tree(),
        [
            Edit(
                path="calc.py",
                action="replace",
                search="def add(a, b):\n    return a - b",
                replace="def add(a, b):\n    return a + b",
            )
        ],
    )
    assert "return a + b" in result.files_after["calc.py"]
    assert result.changed_lines == 2
    assert not result.fuzzy_edits


def test_ambiguous_anchor_reports_lines():
    with pytest.raises(PatchApplyFailed) as info:
        apply_edits(tree(), [Edit(path="calc.py", action="replace", search="    return a - b", replace="x")])
    error = info.value.errors[0]
    assert error.code == "anchor_ambiguous" and "lines 2, 6" in error.message


def test_tolerant_match_ignores_trailing_whitespace():
    result = apply_edits(
        tree(),
        [
            Edit(
                path="calc.py",
                action="replace",
                search="def add(a, b):   \n    return a - b  ",
                replace="def add(a, b):\n    return a + b",
            )
        ],
    )
    assert result.fuzzy_edits == [0]


def test_not_found_quotes_closest_region():
    with pytest.raises(PatchApplyFailed) as info:
        apply_edits(
            tree(),
            [Edit(path="calc.py", action="replace", search="def add(x, y):\n    return x - y", replace="z")],
        )
    assert info.value.errors[0].code == "anchor_not_found"
    assert "line 1" in info.value.errors[0].hint


def test_overlapping_edits_fail():
    with pytest.raises(PatchApplyFailed) as info:
        apply_edits(
            tree(),
            [
                Edit(
                    path="calc.py",
                    action="replace",
                    search="def add(a, b):\n    return a - b",
                    replace="x = 1",
                ),
                Edit(
                    path="calc.py",
                    action="replace",
                    search="add(a, b):\n    return a - b\n\n\ndef sub",
                    replace="y",
                ),
            ],
        )
    assert any(e.code == "overlap" for e in info.value.errors)


def test_crlf_is_preserved():
    crlf = SOURCE.replace(b"\n", b"\r\n")
    result = apply_edits(
        tree({"calc.py": crlf}, {"calc.py": "crlf"}),
        [
            Edit(
                path="calc.py",
                action="replace",
                search="def add(a, b):\n    return a - b",
                replace="def add(a, b):\n    return a + b",
            )
        ],
    )
    new = result.files_after["calc.py"]
    assert "\r\n" in new and "\n" not in new.replace("\r\n", "")
    assert result.changed_lines == 2


def test_create_delete_and_insert():
    base = tree({"calc.py": SOURCE, "old.py": b"x = 1\n"})
    result = apply_edits(
        base,
        [
            Edit(path="new.py", action="create", replace="y = 2\n"),
            Edit(path="old.py", action="delete"),
            Edit(
                path="calc.py",
                action="insert_after",
                search="def sub(a, b):\n    return a - b\n",
                replace="\n\ndef mul(a, b):\n    return a * b\n",
            ),
        ],
    )
    assert result.files_after["new.py"] == "y = 2\n"
    assert result.files_after["old.py"] is None
    assert "def mul" in result.files_after["calc.py"]
    assert result.created == ["new.py"] and result.deleted == ["old.py"]


def test_syntax_error_is_caught_before_running():
    with pytest.raises(PatchApplyFailed) as info:
        apply_edits(
            tree(), [Edit(path="calc.py", action="replace", search="def add(a, b):", replace="def add(a, b)")]
        )
    assert info.value.errors[0].code == "syntax_error"


def test_fingerprint_depends_only_on_resulting_code():
    edit_a = Edit(
        path="calc.py",
        action="replace",
        search="def add(a, b):\n    return a - b",
        replace="def add(a, b):\n    return a + b",
    )
    edit_b = Edit(
        path="calc.py",
        action="replace",
        search="    return a - b\n\n\ndef sub",
        replace="    return a + b\n\n\ndef sub",
    )
    assert apply_edits(tree(), [edit_a]).fingerprint == apply_edits(tree(), [edit_b]).fingerprint


def test_diff_applies_with_git(tmp_path: Path):
    files = {"calc.py": SOURCE, "noeol.py": b"a = 1\nb = 2"}
    base = tree(files)
    result = apply_edits(
        base,
        [
            Edit(
                path="calc.py",
                action="replace",
                search="return a - b\n\n\ndef sub",
                replace="return a + b\n\n\ndef sub",
            ),
            Edit(path="noeol.py", action="replace", search="b = 2", replace="b = 3"),
            Edit(path="added.py", action="create", replace="z = 0\n"),
        ],
    )
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "fix.diff").write_text(unified_diff(base, result.files_after), encoding="utf-8")
    subprocess.run(["git", "apply", "--check", "fix.diff"], cwd=tmp_path, check=True)
    subprocess.run(["git", "apply", "fix.diff"], cwd=tmp_path, check=True)
    assert "return a + b" in (tmp_path / "calc.py").read_text()
    assert (tmp_path / "noeol.py").read_bytes() == b"a = 1\nb = 3"
    assert (tmp_path / "added.py").exists()


def test_redacted_edit_is_flagged_by_policy_helper():
    from fixloop_core.secrets import contains_redaction, redact_text

    text, hits = redact_text('API_KEY = "sk-abcdefghijklmnopqrstuvwxyz123456"\nx = 1\n')
    assert hits and contains_redaction(text) and text.count("\n") == 2
