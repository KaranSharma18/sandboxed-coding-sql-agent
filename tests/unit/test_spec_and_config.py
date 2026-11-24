from pathlib import Path

import pytest
from pydantic import ValidationError

from fixloop_core.config import load_config, normalize_project_name
from fixloop_core.ids import exec_id, short_id, uuid7
from fixloop_core.spec import TaskSpec

ROOT = Path(__file__).resolve().parents[2]


def python_spec(**overrides):
    data = {
        "kind": "python.fix",
        "source": {"type": "inline", "files": {"pkg/a.py": "x = 1\n"}},
        "oracle": {"type": "pytest", "target_tests": ["tests/test_a.py::test_x"]},
    }
    data.update(overrides)
    return data


def test_valid_python_spec_defaults():
    spec = TaskSpec.model_validate(python_spec())
    assert spec.oracle.python_version == "3.12"
    assert spec.options.max_repairs == 3
    assert spec.is_python and not spec.is_sql


@pytest.mark.parametrize(
    "node", ["-p", "tests/../x.py::t", "tests/test_a.py::test x", "/abs/test.py::t", "a.txt::t"]
)
def test_rejects_bad_node_ids(node):
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(python_spec(oracle={"type": "pytest", "target_tests": [node]}))


@pytest.mark.parametrize("path", ["../x.py", "/etc/passwd", "a\\b.py", "a//b.py", "C:/x.py"])
def test_rejects_unsafe_inline_paths(path):
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(python_spec(source={"type": "inline", "files": {path: "x"}}))


def test_kind_must_match_oracle_and_source():
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(python_spec(kind="sql.write"))
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(
            {
                "kind": "sql.fix",
                "source": {"type": "sql_snapshot", "ddl": "create table t(a int);"},
                "oracle": {"type": "sql", "dialect": "duckdb"},
            }
        )  # sql.fix needs broken_query


def test_callback_must_be_https():
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(python_spec(options={"callback_url": "http://example.com/hook"}))


def test_parametrized_node_ids_allowed():
    spec = TaskSpec.model_validate(
        python_spec(oracle={"type": "pytest", "target_tests": ["tests/test_a.py::TestX::test_y[1-a b]"]})
    )
    assert spec.oracle.target_tests[0].endswith("[1-a b]")


def test_config_loads_and_prices_every_routed_model():
    config = load_config(ROOT / "config")
    for role in config.models.roles:
        config.models.price_for(config.models.role(role).model)
    assert "pytest" in config.allowlist
    assert config.models.role("repair", "quality").model == "gemini-3.1-pro-preview"


def test_price_math():
    config = load_config(ROOT / "config")
    price = config.models.price_for("gemini-3.8-flash")
    # 75k input with 25k cached + 6k output: 50k*1.5 + 25k*0.15 + 6k*7.5 per million
    assert price.cost(75_000, 25_000, 6_000, 0) == pytest.approx(0.12375)


def test_name_normalization():
    assert normalize_project_name("Typing_Extensions") == "typing-extensions"
    assert normalize_project_name("zope.interface") == "zope-interface"


def test_ids():
    a, b = uuid7(), uuid7()
    assert a.version == 7 and a != b
    assert len(short_id(a)) == 10
    assert exec_id("t", 1, "candidate", "fp") == exec_id("t", 1, "candidate", "fp")
    assert exec_id("t", 1, "candidate", "fp") != exec_id("t", 2, "candidate", "fp")
