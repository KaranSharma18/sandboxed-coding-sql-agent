from pathlib import Path

import pytest

from fixloop_core.config import load_config
from fixloop_core.outputs import DependencyRequest, Edit
from fixloop_core.patching import WorkingTree, apply_edits
from fixloop_core.policy import check_patch, glob_match, protected_paths_for

CONFIG = load_config(Path(__file__).resolve().parents[2] / "config")
POLICY = CONFIG.policies.patch

BASE = {
    "pkg/core.py": b"def f(x):\n    return x\n",
    "tests/test_core.py": b"from pkg.core import f\n\ndef test_f():\n    assert f(1) == 2\n",
    "pyproject.toml": b'[project]\nname = "pkg"\ndependencies = []\n\n[tool.pytest.ini_options]\naddopts = "-q"\n\n[tool.black]\nline-length = 88\n',
    "requirements.txt": b"requests==2.32.0\n",
}


def check(edits, kind="python.fix", deps=(), allow_deps=False):
    tree = WorkingTree(BASE)
    applied = apply_edits(tree, edits)
    protected = protected_paths_for(POLICY, [], ["tests/test_core.py::test_f"])
    return check_patch(
        tree=tree,
        edits=edits,
        applied=applied,
        policy=POLICY,
        protected=protected,
        task_kind=kind,
        allow_dependency_changes=allow_deps,
        dependency_requests=list(deps),
        allowlist=CONFIG.allowlist,
        target_test_files=["tests/test_core.py"],
    )


def codes(result):
    return {v.code for v in result.violations}


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("tests/a/b.py", "tests/**", True),
        ("test_x.py", "**/test_*.py", True),
        ("a/test_x.py", "**/test_*.py", True),
        ("a/b/conftest.py", "**/conftest.py", True),
        ("src/tests_helper.py", "tests/**", False),
        ("Dockerfile.dev", "Dockerfile*", True),
        ("sub/Dockerfile", "Dockerfile*", False),
    ],
)
def test_glob_match(path, pattern, expected):
    assert glob_match(path, pattern) is expected


def test_clean_fix_passes():
    result = check([Edit(path="pkg/core.py", action="replace", search="return x", replace="return x + 1")])
    assert result.ok, result.feedback()


def test_editing_tests_is_refused():
    result = check([Edit(path="tests/test_core.py", action="replace", search="== 2", replace="== 1")])
    assert "protected_path" in codes(result)


def test_editing_requirements_is_refused():
    result = check([Edit(path="requirements.txt", action="replace", search="2.32.0", replace="2.31.0")])
    assert "protected_infra" in codes(result)


def test_skip_via_alias_is_caught_but_comment_is_fine():
    gaming = check(
        [
            Edit(
                path="pkg/core.py",
                action="replace",
                search="def f(x):\n    return x",
                replace="from pytest import skip as s\n\ndef f(x):\n    s('nope')\n    return x",
            )
        ]
    )
    assert "test_gaming" in codes(gaming)
    comment = check(
        [
            Edit(
                path="pkg/core.py",
                action="replace",
                search="return x",
                replace="return x + 1  # we used to call pytest.skip here",
            )
        ]
    )
    assert comment.ok


def test_detecting_pytest_is_caught():
    result = check(
        [
            Edit(
                path="pkg/core.py",
                action="replace",
                search="return x",
                replace="import os\n    return x + 1 if 'PYTEST_CURRENT_TEST' in os.environ else x",
            )
        ]
    )
    assert "test_gaming" in codes(result)


def test_pyproject_test_table_protected_but_other_tables_editable():
    bad = check(
        [
            Edit(
                path="pyproject.toml",
                action="replace",
                search='addopts = "-q"',
                replace='addopts = "-q -k not_f"',
            )
        ]
    )
    assert "protected_path" in codes(bad)
    good = check(
        [
            Edit(
                path="pyproject.toml",
                action="replace",
                search="line-length = 88",
                replace="line-length = 100",
            )
        ]
    )
    assert good.ok, good.feedback()
    deps = check(
        [
            Edit(
                path="pyproject.toml",
                action="replace",
                search="dependencies = []",
                replace='dependencies = ["x"]',
            )
        ]
    )
    assert "dependency_not_allowed" in codes(deps)


def test_risky_code_is_a_warning_not_a_violation():
    result = check(
        [
            Edit(
                path="pkg/core.py",
                action="replace",
                search="return x",
                replace="import subprocess\n    return x + 1",
            )
        ]
    )
    assert result.ok and any("subprocess" in w for w in result.warnings)


def test_size_limits():
    big = "\n".join(f"v{i} = {i}" for i in range(450)) + "\n"
    result = check([Edit(path="pkg/big.py", action="create", replace=big)])
    assert "patch_too_large" in codes(result)
    assert check([Edit(path="pkg/big.py", action="create", replace=big)], kind="python.implement").ok


def test_dependency_requests():
    request = DependencyRequest(package="python-dateutil", version="2.9.0.post0", reason="parse dates")
    refused = check(
        [Edit(path="pkg/core.py", action="replace", search="return x", replace="return x + 1")],
        deps=[request],
    )
    assert refused.ok and refused.refused_dependencies
    approved = check(
        [Edit(path="pkg/core.py", action="replace", search="return x", replace="return x + 1")],
        deps=[request],
        allow_deps=True,
    )
    assert approved.approved_dependencies == [request]
    unknown = DependencyRequest(package="totally-unknown-pkg", version="1.0", reason="x")
    assert check(
        [Edit(path="pkg/core.py", action="replace", search="return x", replace="return x + 1")],
        deps=[unknown],
        allow_deps=True,
    ).refused_dependencies
